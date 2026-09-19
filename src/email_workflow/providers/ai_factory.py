
from typing import Callable, List, Optional

from email_workflow.models.config import AppConfig, AIMode, APIConfig
from email_workflow.providers.base_ai import AIProvider
from email_workflow.providers.api_providers import (
    PROVIDER_METADATA,
    OpenAICompatibleProvider,
)
from email_workflow.providers.fallback_ai import FallbackAIProvider, ProviderLink
from email_workflow.providers.key_pool import (
    KeyLane,
    KeyPoolProvider,
    all_limiters,
    discover_key_envs,
    parallel_lanes,
)
from email_workflow.providers.local_ai import OllamaProvider
from email_workflow.providers.fake_ai import FakeAIProvider

SUPPORTED = tuple(PROVIDER_METADATA.keys())


def _api_config_for(provider: str, model: Optional[str], key_env: Optional[str],
                    template: APIConfig) -> APIConfig:
    """An APIConfig for one provider, inheriting temperature/token limits."""
    meta = PROVIDER_METADATA.get(provider, {})
    return APIConfig(
        provider=provider,
        model=model or meta.get("default_model") or template.model,
        api_key_env=key_env or meta.get("env_var", f"{provider.upper()}_API_KEY"),
        temperature=template.temperature,
        max_output_tokens=template.max_output_tokens,
    )


def build_provider_chain(
    config: AppConfig, on_switch: Optional[Callable] = None
) -> List[ProviderLink]:
    """The ordered list of providers to try, primary first.

    A provider with no API key is left out entirely: the chain only contains
    things that could actually work.
    """
    primary_cfg = config.ai.api
    keys_cfg = config.ai.keys
    links: List[ProviderLink] = []
    seen_pairs = set()    # (provider, model) - an exact duplicate is pointless
    seen_providers = set()

    def add(provider: str, model: Optional[str], key_env: Optional[str],
            only_if_provider_is_new: bool = False) -> None:
        provider = provider.lower()
        if provider not in SUPPORTED:
            return
        # auto-detected entries only fill gaps; an explicitly listed one may
        # add a second model from a provider already in the chain, which is how
        # "try the cheap model first, then the bigger one" is expressed.
        if only_if_provider_is_new and provider in seen_providers:
            return

        cfg = _api_config_for(provider, model, key_env, primary_cfg)
        if (provider, cfg.model) in seen_pairs:
            return

        # Every key for this provider, not just the first: separate accounts
        # are separate allowances, and the pool uses them all.
        key_envs = discover_key_envs(
            cfg.api_key_env,
            auto_detect=keys_cfg.auto_detect,
            extra=keys_cfg.extra.get(provider, []),
        )
        if not key_envs:
            return

        seen_pairs.add((provider, cfg.model))
        seen_providers.add(provider)

        name = PROVIDER_METADATA[provider]["name"]
        lanes = [
            KeyLane(
                ProviderLink(
                    # The key is named in the label so a switch between two
                    # keys of one provider does not read as the same thing
                    # twice.
                    label=name if len(key_envs) == 1 else f"{name} [{env}]",
                    model=cfg.model,
                    provider=OpenAICompatibleProvider(
                        provider, cfg.model_copy(update={"api_key_env": env})
                    ),
                ),
                env,
            )
            for env in key_envs
        ]

        if len(lanes) == 1:
            links.append(lanes[0].link)
            return

        links.append(
            ProviderLink(
                label=f"{name} x{len(lanes)} keys",
                model=cfg.model,
                provider=KeyPoolProvider(lanes, on_switch=on_switch),
            )
        )

    add(primary_cfg.provider, primary_cfg.model, primary_cfg.api_key_env)

    fb = config.ai.fallback
    if fb.enabled:
        for link in fb.chain:
            add(link.provider, link.model, link.api_key_env)
        if fb.auto_detect:
            for provider in fb.auto_order:
                add(provider, None, None, only_if_provider_is_new=True)

        # Last resort: the model on this machine. Every cloud provider fails
        # together when the connection drops, and they run out of free quota on
        # the same day - a local model has neither problem. It is only ever
        # reached once everything above it has failed, so a machine without
        # Ollama pays nothing for having this here.
        if fb.use_local_last and config.ai.mode != AIMode.LOCAL:
            links.append(
                ProviderLink(
                    label=f"local {config.ai.local.runtime}",
                    model=config.ai.local.model,
                    provider=OllamaProvider(config.ai.local),
                )
            )

    return links


def get_ai_provider(
    config: AppConfig,
    on_switch: Optional[Callable] = None,
    on_throttle: Optional[Callable] = None,
) -> AIProvider:
    """Instantiate and validate the AI provider described by the config.

    on_switch(from_link, to_link, error) is called when the chain moves on
    because a provider ran out of quota or stopped working.
    on_throttle(seconds, requests_per_minute) is called when a request has
    to wait to stay under a per-minute limit.
    """
    if config.ai.mode == AIMode.LOCAL:
        provider: AIProvider = OllamaProvider(config.ai.local)
        provider.validate_setup()
        return provider

    prov_name = config.ai.api.provider.lower()

    if prov_name == "fake":
        provider = FakeAIProvider()
        provider.validate_setup()
        return provider

    if prov_name not in SUPPORTED:
        raise ValueError(
            f"Unsupported AI provider '{prov_name}' in config. "
            f"Supported: {', '.join(SUPPORTED)}, fake"
        )

    links = build_provider_chain(config, on_switch=on_switch)

    if on_throttle:
        for link in links:
            for limiter in all_limiters(link.provider):
                limiter.on_wait = on_throttle

    if not links:
        meta = PROVIDER_METADATA[prov_name]
        raise ValueError(
            f"No API key found for {meta['name']}. Set '{meta['env_var']}' in your "
            f".env file, or run 'email-workflow setup'. Get a key at {meta['keys_url']}."
        )

    if len(links) == 1:
        links[0].provider.validate_setup()
        return links[0].provider

    chain = FallbackAIProvider(links, on_switch=on_switch)
    chain.validate_setup()
    return chain
