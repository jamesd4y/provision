"""Backup invariants.

The failure mode that matters is not "the backup broke" — you notice that. It
is "the backup ran and captured something that will not restore".
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from lib import model  # noqa: E402


def test_every_stack_with_data_has_decided_about_backup(repo):
    undecided = [
        name for name, service in repo.services.items()
        if service.data_dirs and not service.backup
    ]
    assert undecided == [], f"these stacks store data but say nothing about backup: {undecided}"


def test_opting_out_requires_a_reason(repo):
    for name, service in repo.services.items():
        if service.backup and not service.backup.get("enabled", True):
            assert service.backup.get("reason"), f"{name} opts out of backup silently"


def test_a_database_is_dumped_rather_than_copied(repo):
    """Copying a live Postgres data directory gives a torn page, not a backup."""
    postgres = repo.services["postgres"]
    assert postgres.backup_paths == [], (
        "postgres must not back up its live data directory — the pre hook dumps it"
    )
    assert any("pg_dumpall" in cmd for cmd in postgres.backup["pre"])

    plan = next(e for e in model.backup_plan(repo, repo.hosts["db01"]) if e["stack"] == "postgres")
    assert plan["paths"] == [plan["staging"]], (
        "the dump must be what gets captured, and nothing else"
    )


def test_an_explicit_empty_paths_list_is_not_treated_as_unset(repo):
    """`paths: []` means none of the data directory, not 'fall back to all of it'."""
    postgres = repo.services["postgres"]
    assert "paths" in postgres.backup
    assert postgres.backup_paths == []
    assert postgres.data_dirs == ["data"]


def test_a_pre_hook_output_is_always_captured(repo):
    for host in repo.hosts.values():
        for entry in model.backup_plan(repo, host):
            if entry["pre"]:
                assert entry["staging"] in entry["paths"], (
                    f"{host.name}/{entry['stack']} runs a pre hook but never backs up its output"
                )


def test_excludes_are_anchored_to_the_stack(repo):
    """An unanchored pattern can match a path component anywhere and silently
    exclude everything — which is exactly what it looks like when it works."""
    for host in repo.hosts.values():
        for entry in model.backup_plan(repo, host):
            for pattern in entry["exclude"]:
                assert pattern.startswith("/"), (
                    f"{entry['stack']} has an unanchored exclude: {pattern}"
                )
                assert pattern.startswith(entry["data_root"]), (
                    f"{entry['stack']} excludes something outside its own data: {pattern}"
                )


def test_hosts_with_backed_up_stacks_run_the_role(repo):
    for host in repo.hosts.values():
        if not host.enabled:
            continue
        if model.backup_plan(repo, host):
            assert "backup" in host.roles, f"{host.name} has data to back up but no backup role"


def test_repository_is_per_host(repo):
    """Shared repositories mean one host can read and rewrite another's history."""
    url = repo.backup["repository"]["url"]
    assert "{host}" in url, "the repository URL must vary per host"


def test_backup_config_holds_keys_not_credentials(repo):
    password = repo.backup["password"]
    assert model.SECRET_REF.match(password)
    assert len(password) < 64 and " " not in password, "that looks like a password, not a key"


def test_retention_keeps_something(repo):
    retention = repo.backup["retention"]
    assert sum(retention.values()) > 0, "a retention policy that keeps nothing is a delete job"


def test_verification_actually_reads_data_back(repo):
    """`restic check` alone only validates structure, not content."""
    assert repo.backup.get("check_read_data_subset"), (
        "set check_read_data_subset, or the integrity check never reads a byte"
    )
