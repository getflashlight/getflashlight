"""Databricks Asset Bundle (DAB) YAML parser and modifier.

Uses ruamel.yaml for round-trip editing that preserves comments and formatting.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from auralake.core.exceptions import DABParserError
from auralake.models.config import AuraLakeConfig
from auralake.models.dab import BundleInfo, DABBundle, DABClusterConfig, DABDiff, DABJobConfig
from auralake.providers.base import AbstractConfigFormat


class DABParser:
    """Parses and modifies Databricks Asset Bundle YAML files."""

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path
        self._yaml = YAML()
        self._yaml.preserve_quotes = True

    def discover_bundles(self) -> list[BundleInfo]:
        """Find all databricks.yml files in the repo."""
        bundles = []
        for yml_path in self.repo_path.rglob("databricks.yml"):
            rel = yml_path.relative_to(self.repo_path)
            bundles.append(BundleInfo(
                name=yml_path.parent.name,
                path=str(rel.parent),
                environment=yml_path.parent.name,
            ))
        return bundles

    def parse_bundle(self, bundle_path: Path) -> DABBundle:
        """Parse a bundle into structured data."""
        yml_file = bundle_path / "databricks.yml"
        if not yml_file.exists():
            raise DABParserError(f"No databricks.yml found at {bundle_path}")
        try:
            with open(yml_file) as f:
                data = self._yaml.load(f) or {}
            return DABBundle(
                name=data.get("bundle", {}).get("name", bundle_path.name),
                path=str(bundle_path),
                raw_config=dict(data) if data else {},
                environments=list(data.get("targets", {}).keys()) if data.get("targets") else [],
            )
        except Exception as exc:
            raise DABParserError(f"Failed to parse {yml_file}: {exc}") from exc

    def get_jobs(self, bundle: DABBundle) -> list[DABJobConfig]:
        """Extract all job definitions from a bundle."""
        jobs = []
        bundle_path = Path(bundle.path)

        # Check main config
        resources = bundle.raw_config.get("resources", {})
        for job_key, job_config in resources.get("jobs", {}).items():
            jobs.append(self._parse_job(job_key, job_config, str(bundle_path / "databricks.yml")))

        # Check resource YAML files in resources/jobs/
        jobs_dir = bundle_path / "resources" / "jobs"
        if jobs_dir.exists():
            for yml_file in jobs_dir.glob("*.yml"):
                try:
                    with open(yml_file) as f:
                        data = self._yaml.load(f) or {}
                    for job_key, job_config in data.get("resources", {}).get("jobs", {}).items():
                        jobs.append(self._parse_job(job_key, job_config, str(yml_file)))
                except Exception:
                    continue
        return jobs

    def get_clusters(self, bundle: DABBundle) -> list[DABClusterConfig]:
        """Extract all cluster definitions from a bundle."""
        clusters = []
        bundle_path = Path(bundle.path)

        resources = bundle.raw_config.get("resources", {})
        for key, config in resources.get("clusters", {}).items():
            clusters.append(self._parse_cluster(key, config, str(bundle_path / "databricks.yml")))

        clusters_dir = bundle_path / "resources" / "clusters"
        if clusters_dir.exists():
            for yml_file in clusters_dir.glob("*.yml"):
                try:
                    with open(yml_file) as f:
                        data = self._yaml.load(f) or {}
                    for key, config in data.get("resources", {}).get("clusters", {}).items():
                        clusters.append(self._parse_cluster(key, config, str(yml_file)))
                except Exception:
                    continue
        return clusters

    def modify_job(self, file_path: Path, job_key: str, changes: dict[str, Any]) -> DABDiff:
        """Modify a job config in-place, return diff. Preserves YAML formatting."""
        try:
            original = file_path.read_text()
            with open(file_path) as f:
                data = self._yaml.load(f)

            jobs = data.get("resources", {}).get("jobs", {})
            if job_key not in jobs:
                raise DABParserError(f"Job '{job_key}' not found in {file_path}")

            self._deep_merge(jobs[job_key], changes)

            import io
            buf = io.StringIO()
            self._yaml.dump(data, buf)
            modified = buf.getvalue()

            file_path.write_text(modified)

            return DABDiff(
                file_path=str(file_path),
                original_content=original,
                modified_content=modified,
                description=f"Modified job '{job_key}': {list(changes.keys())}",
            )
        except DABParserError:
            raise
        except Exception as exc:
            raise DABParserError(f"Failed to modify job '{job_key}' in {file_path}: {exc}") from exc

    def modify_cluster(self, file_path: Path, cluster_key: str, changes: dict[str, Any]) -> DABDiff:
        """Modify a cluster config in-place, return diff."""
        try:
            original = file_path.read_text()
            with open(file_path) as f:
                data = self._yaml.load(f)

            clusters = data.get("resources", {}).get("clusters", {})
            if cluster_key not in clusters:
                raise DABParserError(f"Cluster '{cluster_key}' not found in {file_path}")

            self._deep_merge(clusters[cluster_key], changes)

            import io
            buf = io.StringIO()
            self._yaml.dump(data, buf)
            modified = buf.getvalue()

            file_path.write_text(modified)

            return DABDiff(
                file_path=str(file_path),
                original_content=original,
                modified_content=modified,
                description=f"Modified cluster '{cluster_key}': {list(changes.keys())}",
            )
        except DABParserError:
            raise
        except Exception as exc:
            raise DABParserError(f"Failed to modify cluster '{cluster_key}': {exc}") from exc

    def add_job_to_cluster(self, file_path: Path, job_key: str, shared_cluster_key: str) -> DABDiff:
        """Reassign a job from its own cluster to a shared cluster."""
        return self.modify_job(file_path, job_key, {
            "tasks": [{"existing_cluster_id": f"${{resources.clusters.{shared_cluster_key}.id}}"}],
        })

    def _parse_job(self, key: str, config: dict, file_path: str) -> DABJobConfig:
        tasks = config.get("tasks", [])
        cluster_key = None
        for task in tasks:
            if "existing_cluster_id" in task:
                ref = task["existing_cluster_id"]
                if "${resources.clusters." in str(ref):
                    cluster_key = str(ref).split("${resources.clusters.")[1].split(".")[0]
                break

        return DABJobConfig(
            key=key,
            name=config.get("name", key),
            file_path=file_path,
            cluster_key=cluster_key,
            schedule=config.get("schedule"),
            tasks=tasks,
            raw_config=dict(config),
        )

    def _parse_cluster(self, key: str, config: dict, file_path: str) -> DABClusterConfig:
        return DABClusterConfig(
            key=key,
            name=config.get("cluster_name", key),
            file_path=file_path,
            node_type_id=config.get("node_type_id"),
            num_workers=config.get("num_workers"),
            autoscale=config.get("autoscale"),
            spark_conf=config.get("spark_conf", {}),
            raw_config=dict(config),
        )

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> None:
        """Recursively merge override into base."""
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                DABParser._deep_merge(base[key], value)
            else:
                base[key] = value


class DABConfigFormat(AbstractConfigFormat):
    """AbstractConfigFormat implementation for Databricks Asset Bundles."""

    def __init__(self, config: AuraLakeConfig) -> None:
        self._config = config
        local_path = config.github.local_path
        self._parser = DABParser(Path(local_path)) if local_path else None

    def parse(self, path: Path) -> dict[str, Any]:
        yaml = YAML()
        with open(path) as f:
            return dict(yaml.load(f) or {})

    def modify_job(self, path: Path, job_name: str, changes: dict[str, Any]) -> str:
        if not self._parser:
            raise DABParserError("No local repo path configured")
        diff = self._parser.modify_job(path, job_name, changes)
        return diff.modified_content

    def modify_cluster(self, path: Path, cluster_name: str, changes: dict[str, Any]) -> str:
        if not self._parser:
            raise DABParserError("No local repo path configured")
        diff = self._parser.modify_cluster(path, cluster_name, changes)
        return diff.modified_content
