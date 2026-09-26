"""Level 1 - relevance scoring and threshold filtering (PRD §8)."""

from __future__ import annotations

import pytest
from tests.conftest import make_company

from jobmonitor.config import FilterSettings
from jobmonitor.filtering import DEFAULT_MODEL, JobFilter, RelevanceModel, score_job
from jobmonitor.models.company import Priority
from jobmonitor.models.job import Job


def job(title: str, **kwargs: object) -> Job:
    payload: dict[str, object] = {
        "company": "TestCo",
        "title": title,
        "url": "https://job-boards.greenhouse.io/testco/jobs/1",
        "source": "greenhouse",
    }
    payload.update(kwargs)
    return Job(**payload)  # type: ignore[arg-type]


def score(title: str, **kwargs: object) -> int:
    return score_job(job(title, **kwargs)).score


class TestTargetRoles:
    """Every primary role from PRD §2 must clear the notify threshold (55)."""

    @pytest.mark.parametrize(
        "title",
        [
            "Software Engineer Intern",
            "Software Engineer Intern, Summer 2027",
            "Software Developer Intern",
            "Backend Engineer Intern",
            "Infrastructure Engineer Intern",
            "Systems Engineer Intern",
            "Platform Engineer Intern",
            "Cloud Engineer Intern",
            "Site Reliability Engineer Intern",
            "Developer Productivity Intern",
            "DevTools Engineering Intern",
            "Machine Learning Engineer Intern",
            "AI Engineer Intern",
            "Data Engineer Intern",
            "Security Engineer Intern",
            "Quantitative Developer Intern",
            "Firmware Engineer Intern",
            "Embedded Software Intern",
            "Robotics Software Intern",
            "Research Engineer Intern",
            "SWE Intern",
            "Software Engineering Co-op",
            "Compiler Engineering Intern",
            "Computer Vision Intern",
            "Quantitative Research Summer Analyst",
        ],
    )
    def test_primary_roles_are_notifiable(self, title: str) -> None:
        assert score(title) >= 55, f"{title} scored {score(title)}"

    def test_the_canonical_example_scores_well(self) -> None:
        assert score("Software Engineer Intern") >= 70


class TestIrrelevantRoles:
    @pytest.mark.parametrize(
        "title",
        [
            "Marketing Intern",
            "Social Media Marketing Intern",
            "Human Resources Intern",
            "Talent Acquisition Intern",
            "Accounting Intern",
            "Tax Intern",
            "Legal Intern",
            "Sales Development Intern",
            "Account Executive Intern",
            "Nursing Intern",
            "Civil Engineer Intern",
            "Construction Management Intern",
            "Graphic Design Intern",
            "Customer Service Intern",
            "Supply Chain Intern",
        ],
    )
    def test_non_technical_internships_are_dropped(self, title: str) -> None:
        assert score(title) < 35, f"{title} scored {score(title)}"

    @pytest.mark.parametrize(
        "title",
        [
            "Senior Software Engineer",
            "Staff Engineer, Platform",
            "Engineering Manager",
            "Director of Engineering",
            "Head of Infrastructure",
            "Principal Software Engineer",
        ],
    )
    def test_non_internships_are_dropped(self, title: str) -> None:
        assert score(title) < 35, f"{title} scored {score(title)}"

    def test_a_senior_role_mentioning_interns_is_still_dropped(self) -> None:
        # Mentoring interns is a common line in senior job descriptions.
        assert (
            score(
                "Senior Software Engineer",
                description="You will mentor our summer internship cohort.",
            )
            < 35
        )


class TestAdjacentRoles:
    """PRD §2/§8: be permissive enough not to miss strong adjacent roles."""

    @pytest.mark.parametrize(
        "title",
        [
            "Data Science Intern",
            "Data Analyst Intern",
            "QA Engineer Intern",
            "Test Engineering Intern",
            "Hardware Engineer Intern",
            "Electrical Engineering Intern",
            "Technical Program Management Intern",
            "Solutions Engineer Intern",
            "Product Manager Intern",
        ],
    )
    def test_adjacent_roles_are_kept(self, title: str) -> None:
        assert score(title) >= 35, f"{title} scored {score(title)}"

    def test_adjacent_roles_score_below_core_roles(self) -> None:
        assert score("Data Analyst Intern") < score("Data Engineer Intern")

    def test_uncertain_roles_are_retained_rather_than_discarded(self) -> None:
        # The PRD's explicit tie-breaker: keep, don't silently drop.
        decision = JobFilter().evaluate(job("Technology Intern"))
        assert decision.keep is True
        assert decision.notify is False


class TestRoleVersusQualifier:
    """A non-technical *team* must not discard a technical *role* (PRD §2)."""

    def test_non_technical_team_qualifier_is_penalised_lightly(self) -> None:
        # A real title from the seed data: a data science internship in an ads org.
        result = score_job(job("Data Science Intern - Influencer Marketing AI Application"))
        assert result.score >= 35, result.reasons
        assert any("team qualifier" in reason for reason in result.reasons)

    def test_non_technical_role_name_is_penalised_fully(self) -> None:
        result = score_job(job("Marketing Intern - Engineering Org"))
        assert result.score < 35
        assert any("non-technical role" in reason for reason in result.reasons)

    def test_supply_chain_team_does_not_discard_a_data_role(self) -> None:
        assert score("Data Science Intern - TikTok Shop Supply Chain & Logistics") >= 35

    def test_supply_chain_as_the_role_is_still_dropped(self) -> None:
        assert score("Supply Chain Intern") < 35

    @pytest.mark.parametrize(
        ("title", "head"),
        [
            ("Software Engineer Intern - ML Platform", "Software Engineer Intern"),
            ("Data Science Intern, Ads", "Data Science Intern"),
            ("SWE Intern (Summer 2027)", "SWE Intern"),
            ("Backend Intern \u2013 Payments", "Backend Intern"),
            ("Plain Title", "Plain Title"),
        ],
    )
    def test_title_splitting(self, title: str, head: str) -> None:
        from jobmonitor.filtering.relevance import split_role_and_qualifier

        assert split_role_and_qualifier(title)[0] == head


class TestWordBoundaries:
    def test_internal_does_not_look_like_an_internship(self) -> None:
        # Substring matching would read "intern" out of "Internal"/"International".
        assert score_job(job("Internal Audit Analyst")).is_internship is False
        assert score_job(job("International Operations Associate")).is_internship is False

    def test_student_is_recognised_as_an_internship_signal(self) -> None:
        # Common in the seed data: "Software Development Student - January 2027".
        result = score_job(job("Software Development Student - January 2027"))
        assert result.is_internship
        assert result.score >= 55

    def test_chip_architecture_roles_are_not_mistaken_for_building_architecture(self) -> None:
        assert score("Clock Design & Architecture Intern") >= 35


class TestConditionalPenalties:
    def test_mechanical_engineering_intern_is_dropped(self) -> None:
        assert score("Mechanical Engineer Intern") < 35

    def test_mechanical_plus_software_content_is_kept(self) -> None:
        # PRD §2: include when technical content makes the role relevant.
        assert score("Mechanical Engineer Intern - Embedded Software") >= 55

    def test_employment_type_can_supply_the_internship_signal(self) -> None:
        assert score("Software Engineer, University Program", employment_type="Intern") >= 55

    def test_internship_signal_in_description_only_still_counts(self) -> None:
        result = score_job(
            job("Software Engineer, Early Career", description="This is a summer internship.")
        )
        assert result.is_internship
        assert result.score >= 55


class TestScoreBounds:
    def test_scores_never_leave_the_0_100_range(self) -> None:
        titles = [
            "Senior Director of Marketing and Sales and Legal and HR",
            "Software Engineer Intern Backend Infrastructure Platform Systems ML AI Security",
            "",
        ]
        for title in titles:
            result = score_job(job(title or "x"))
            assert 0 <= result.score <= 100

    def test_stacked_negatives_are_capped(self) -> None:
        # Capping stops the score going wildly negative and keeps it explainable.
        assert score("Marketing and Sales and Recruiting and Accounting Intern") == 0


class TestExplanations:
    def test_reasons_are_recorded_for_a_match(self) -> None:
        result = score_job(job("Software Engineer Intern"))
        assert any("internship signal" in reason for reason in result.reasons)
        assert any("core technical role" in reason for reason in result.reasons)
        assert "software engineer" in result.matched_core

    def test_reasons_are_recorded_for_a_rejection(self) -> None:
        result = score_job(job("Marketing Intern"))
        assert any("non-technical role" in reason for reason in result.reasons)
        assert "marketing" in result.matched_negative

    def test_explanation_is_a_readable_string(self) -> None:
        assert "internship signal" in score_job(job("Software Engineer Intern")).explanation

    def test_no_signals_explanation(self) -> None:
        assert score_job(job("Zzzz Qqqq")).explanation == "no signals matched"


class TestCompanyPriority:
    def test_high_priority_companies_score_higher_than_experimental(self) -> None:
        title = job("Software Engineer Intern")
        high = score_job(title, make_company(priority=Priority.HIGH)).score
        experimental = score_job(title, make_company(priority=Priority.EXPERIMENTAL)).score
        assert high > experimental

    def test_priority_bonus_cannot_rescue_an_irrelevant_role(self) -> None:
        result = score_job(job("Marketing Intern"), make_company(priority=Priority.HIGH))
        assert result.score < 35


class TestThresholds:
    def test_default_thresholds_partition_keep_and_notify(self) -> None:
        job_filter = JobFilter()
        notifiable = job_filter.evaluate(job("Software Engineer Intern"))
        kept_only = job_filter.evaluate(job("Technology Intern"))
        dropped = job_filter.evaluate(job("Marketing Intern"))
        assert (notifiable.keep, notifiable.notify) == (True, True)
        assert (kept_only.keep, kept_only.notify) == (True, False)
        assert (dropped.keep, dropped.notify) == (False, False)

    def test_notify_threshold_is_configurable(self) -> None:
        strict = JobFilter(FilterSettings(notify_threshold=95, keep_threshold=10))
        assert strict.evaluate(job("Software Engineer Intern")).notify is False
        lenient = JobFilter(FilterSettings(notify_threshold=20, keep_threshold=10))
        assert lenient.evaluate(job("Data Analyst Intern")).notify is True

    def test_keep_threshold_is_configurable(self) -> None:
        permissive = JobFilter(FilterSettings(notify_threshold=90, keep_threshold=1))
        assert permissive.evaluate(job("Marketing Intern")).keep is True

    def test_notify_implies_keep(self) -> None:
        job_filter = JobFilter()
        for title in ["Software Engineer Intern", "Marketing Intern", "Data Analyst Intern"]:
            decision = job_filter.evaluate(job(title))
            assert not decision.notify or decision.keep


class TestImmediateAlerts:
    def test_high_priority_companies_alert_immediately(self) -> None:
        decision = JobFilter().evaluate(
            job("Software Engineer Intern"), make_company(priority=Priority.HIGH)
        )
        assert decision.immediate is True

    def test_lower_priority_companies_are_batched(self) -> None:
        decision = JobFilter().evaluate(
            job("Software Engineer Intern"), make_company(priority=Priority.MEDIUM)
        )
        assert decision.notify is True
        assert decision.immediate is False

    def test_immediate_priorities_are_configurable(self) -> None:
        job_filter = JobFilter(FilterSettings(immediate_alert_priorities=("high", "medium")))
        decision = job_filter.evaluate(
            job("Software Engineer Intern"), make_company(priority=Priority.MEDIUM)
        )
        assert decision.immediate is True

    def test_a_non_notifiable_job_is_never_immediate(self) -> None:
        decision = JobFilter().evaluate(
            job("Marketing Intern"), make_company(priority=Priority.HIGH)
        )
        assert decision.immediate is False


class TestBatchHelpers:
    def test_evaluate_all_preserves_order(self) -> None:
        jobs = [job("Software Engineer Intern"), job("Marketing Intern"), job("ML Engineer Intern")]
        decisions = JobFilter().evaluate_all(jobs)
        assert [d.job.title for d in decisions] == [j.title for j in jobs]

    def test_relevant_drops_below_keep_threshold(self) -> None:
        jobs = [job("Software Engineer Intern"), job("Marketing Intern")]
        kept = JobFilter().relevant(jobs)
        assert [d.job.title for d in kept] == ["Software Engineer Intern"]

    def test_empty_input(self) -> None:
        assert JobFilter().relevant([]) == []


class TestModelIsConfigurable:
    def test_weights_can_be_overridden(self) -> None:
        generous = RelevanceModel(internship_base=90, core_role_bonus=10)
        assert score_job(job("Software Engineer Intern"), model=generous).score == 100

    def test_signal_lists_can_be_extended(self) -> None:
        custom = RelevanceModel(core_role_signals=(*DEFAULT_MODEL.core_role_signals, "widgetry"))
        assert score_job(job("Widgetry Intern"), model=custom).score >= 55
        assert score_job(job("Widgetry Intern")).score < 55

    def test_default_model_is_shared_and_immutable(self) -> None:
        with pytest.raises((AttributeError, TypeError)):
            DEFAULT_MODEL.internship_base = 1  # type: ignore[misc]


class TestRealisticFixtureBehaviour:
    """Run the filter over the greenhouse fixture, as the pipeline will."""

    def test_only_the_technical_internships_survive(self) -> None:
        from tests.scrapers.conftest import case_for

        jobs = case_for("greenhouse").source().fetch_jobs()
        kept = {d.job.title for d in JobFilter().relevant(jobs, make_company())}
        assert "Software Engineer Intern, Summer 2027" in kept
        assert "Machine Learning Engineer Intern" in kept
        assert "Marketing Intern" not in kept
        assert "Senior Staff Software Engineer" not in kept
