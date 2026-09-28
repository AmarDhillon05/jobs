"""Level 1 - company registry model, and shape checks on the real companies.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobmonitor.models.company import (
    Company,
    CompanyRegistry,
    Priority,
    RegistryError,
    SupportStatus,
    default_registry_path,
    load_default_registry,
)


def make_company(name: str = "Acme", **kwargs: object) -> Company:
    defaults: dict[str, object] = {
        "company": name,
        "careers_url": "https://job-boards.greenhouse.io/acme",
        "industry": "Developer Infrastructure",
        "priority": Priority.HIGH,
        "provider": "greenhouse",
        "provider_config": {"board_token": "acme"},
        "support_status": SupportStatus.SUPPORTED,
    }
    defaults.update(kwargs)
    return Company(**defaults)  # type: ignore[arg-type]


class TestCompany:
    def test_key_combines_name_and_provider(self) -> None:
        assert make_company().key == "Acme::greenhouse"

    def test_pollable_statuses(self) -> None:
        assert make_company(support_status=SupportStatus.SUPPORTED).is_pollable
        assert make_company(support_status=SupportStatus.PARTIAL).is_pollable
        assert not make_company(support_status=SupportStatus.BLOCKED).is_pollable
        assert not make_company(support_status=SupportStatus.RESEARCH_NEEDED).is_pollable

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(RegistryError, match="must not be empty"):
            make_company(company="   ")

    def test_empty_provider_rejected(self) -> None:
        with pytest.raises(RegistryError, match="provider must not be empty"):
            make_company(provider="")

    def test_relative_careers_url_rejected(self) -> None:
        with pytest.raises(RegistryError, match="absolute http"):
            make_company(careers_url="/careers")

    def test_round_trips_through_dict(self) -> None:
        original = make_company(aliases=("Acme Inc",), last_validated="2026-09-26")
        assert Company.from_dict(original.to_dict()) == original

    def test_from_dict_defaults(self) -> None:
        company = Company.from_dict(
            {
                "company": "Bare",
                "provider": "lever",
                "careers_url": "https://jobs.lever.co/bare",
            }
        )
        assert company.priority is Priority.MEDIUM
        assert company.support_status is SupportStatus.RESEARCH_NEEDED
        assert company.provider_config == {}
        assert company.industry == "unknown"

    @pytest.mark.parametrize("missing", ["company", "provider", "careers_url"])
    def test_from_dict_missing_required_key(self, missing: str) -> None:
        data = {
            "company": "X",
            "provider": "lever",
            "careers_url": "https://jobs.lever.co/x",
        }
        del data[missing]
        with pytest.raises(RegistryError, match="missing required key"):
            Company.from_dict(data)

    def test_from_dict_rejects_unknown_enums(self) -> None:
        base = {"company": "X", "provider": "lever", "careers_url": "https://jobs.lever.co/x"}
        with pytest.raises(RegistryError, match="unknown priority"):
            Company.from_dict({**base, "priority": "urgent"})
        with pytest.raises(RegistryError, match="unknown support_status"):
            Company.from_dict({**base, "support_status": "maybe"})


class TestCompanyRegistry:
    def _registry(self) -> CompanyRegistry:
        return CompanyRegistry(
            (
                make_company("Alpha", provider="greenhouse"),
                make_company(
                    "Beta",
                    provider="lever",
                    careers_url="https://jobs.lever.co/beta",
                    aliases=("Beta Corp",),
                ),
                make_company("Gamma", provider="lever", support_status=SupportStatus.BLOCKED),
                make_company(
                    "Delta", provider="ashby", support_status=SupportStatus.RESEARCH_NEEDED
                ),
                make_company("Epsilon", provider="ashby", support_status=SupportStatus.PARTIAL),
            )
        )

    def test_len_and_iteration(self) -> None:
        registry = self._registry()
        assert len(registry) == 5
        assert next(c.company for c in registry) == "Alpha"

    def test_duplicate_names_rejected_case_insensitively(self) -> None:
        with pytest.raises(RegistryError, match="duplicate company"):
            CompanyRegistry((make_company("Acme"), make_company("acme")))

    def test_get_by_name_and_alias(self) -> None:
        registry = self._registry()
        assert registry.get("beta") is not None
        assert registry.get("Beta Corp") is not None
        assert registry.get("nobody") is None

    def test_pollable_excludes_blocked_and_research_needed(self) -> None:
        names = [c.company for c in self._registry().pollable()]
        assert names == ["Alpha", "Beta", "Epsilon"]

    def test_by_provider_and_with_status(self) -> None:
        registry = self._registry()
        assert [c.company for c in registry.by_provider("lever")] == ["Beta", "Gamma"]
        assert [c.company for c in registry.with_status(SupportStatus.BLOCKED)] == ["Gamma"]

    def test_shard_splits_only_pollable_companies(self) -> None:
        shards = self._registry().shard(2)
        assert [[c.company for c in shard] for shard in shards] == [
            ["Alpha", "Beta"],
            ["Epsilon"],
        ]

    def test_shard_of_one_gives_one_company_per_shard(self) -> None:
        assert all(len(shard) == 1 for shard in self._registry().shard(1))

    def test_shard_larger_than_registry_gives_single_shard(self) -> None:
        assert len(self._registry().shard(500)) == 1

    def test_shard_size_must_be_positive(self) -> None:
        with pytest.raises(RegistryError, match="shard size"):
            self._registry().shard(0)

    def test_counts(self) -> None:
        registry = self._registry()
        assert registry.counts_by_status()["supported"] == 2
        assert registry.counts_by_status()["blocked"] == 1
        assert registry.counts_by_provider() == {"ashby": 2, "lever": 2, "greenhouse": 1}

    def test_load_and_dump_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "companies.json"
        self._registry().dump(path)
        assert CompanyRegistry.load(path).to_list() == self._registry().to_list()

    def test_load_rejects_non_list_json(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"company": "X"}), encoding="utf-8")
        with pytest.raises(RegistryError, match="expected a JSON list"):
            CompanyRegistry.load(path)


class TestShippedRegistry:
    """Guards on the real companies.json that ships with the repo (PRD §4.3, §32)."""

    @pytest.fixture(scope="class")
    @classmethod
    def registry(cls) -> CompanyRegistry:
        return load_default_registry()

    def test_registry_file_exists_at_repo_root(self) -> None:
        assert default_registry_path().is_file()

    def test_monitors_a_focused_but_substantial_set_of_companies(
        self, registry: CompanyRegistry
    ) -> None:
        # PRD §1 asks for "approximately 100-150 high-value companies". The user
        # then grew the list to 183 and trimmed it to employers at or above an AWS
        # SDE internship in resume value (91 polled): quality over breadth, by
        # choice. The floor still catches a build that silently loses most of the
        # registry; the ceiling one that balloons.
        assert 50 <= len(registry.pollable()) <= 200

    def test_every_entry_records_where_it_was_discovered(self, registry: CompanyRegistry) -> None:
        for company in registry:
            assert company.source_discovered_from, company.company

    def test_pollable_companies_carry_every_config_key_their_adapter_requires(
        self, registry: CompanyRegistry
    ) -> None:
        # An adapter serving exactly one company (Amazon, Google, Goldman, IBM,
        # Atlassian) requires no keys, so an empty config is correct for it.
        from jobmonitor.scrapers import source_class_for

        for company in registry.pollable():
            required = source_class_for(company.provider).required_config
            missing = [key for key in required if not company.provider_config.get(key)]
            assert not missing, f"{company.company}: missing {missing}"

    def test_no_duplicate_provider_configs(self, registry: CompanyRegistry) -> None:
        """Two companies pointing at one board would double-report the same jobs."""
        seen: dict[tuple[str, str], str] = {}
        for company in registry.pollable():
            key = (company.provider, json.dumps(company.provider_config, sort_keys=True))
            assert key not in seen, f"{company.company} duplicates {seen.get(key)}"
            seen[key] = company.company

    def test_priorities_and_industries_are_populated(self, registry: CompanyRegistry) -> None:
        for company in registry:
            assert company.industry and company.industry != "unknown", company.company
            assert isinstance(company.priority, Priority)

    def test_has_high_priority_coverage_across_several_industries(
        self, registry: CompanyRegistry
    ) -> None:
        industries = {c.industry for c in registry if c.priority is Priority.HIGH}
        assert len(industries) >= 15

    def test_uses_several_reusable_providers_not_one(self, registry: CompanyRegistry) -> None:
        # PRD §6: reusable adapters, not 150 bespoke scrapers.
        providers = registry.counts_by_provider()
        assert len([p for p, n in providers.items() if n >= 3]) >= 4

    def test_research_needed_entries_explain_themselves(self, registry: CompanyRegistry) -> None:
        for company in registry.with_status(SupportStatus.RESEARCH_NEEDED):
            assert company.notes, company.company

    def test_shards_evenly_for_the_default_shard_size(self, registry: CompanyRegistry) -> None:
        shards = registry.shard(8)
        assert sum(len(shard) for shard in shards) == len(registry.pollable())
        assert all(1 <= len(shard) <= 8 for shard in shards)
