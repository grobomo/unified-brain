"""Project registry — maps repos, Teams chats, people, and configs to projects.

Shared reference between unified-brain and ccc-manager.
Loaded from a YAML file (projects.yaml in the data/ or config/ directory).

Example projects.yaml:
    projects:
      hackathon26:
        repos: [grobomo/hackathon26, altarr/boothapp]
        teams_chats: ["19:abc...@thread.v2"]
        people: [alice, bob]
        manager_config: config/hackathon26.yaml
        worker_type: k8s
        k8s_namespace: hackathon
      claude-portable:
        repos: [grobomo/claude-portable]
        teams_chats: []
        manager_config: config/claude-portable.yaml
        worker_type: ec2
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# YAML is optional — fall back to JSON if pyyaml not installed
try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


class ProjectRegistry:
    """Maps repos, chats, and people to projects."""

    def __init__(self, config_path: str = "config/projects.yaml"):
        self.config_path = Path(config_path)
        self.projects: dict[str, dict] = {}
        self._repo_index: dict[str, str] = {}    # repo -> project name
        self._chat_index: dict[str, str] = {}    # chat_id -> project name
        self._person_index: dict[str, str] = {}  # person -> project name
        self.load()

    def load(self):
        """Load registry from YAML or JSON file."""
        if not self.config_path.exists():
            logger.warning(f"Registry not found: {self.config_path}")
            return

        text = self.config_path.read_text()
        if self.config_path.suffix in (".yaml", ".yml") and _HAS_YAML:
            data = yaml.safe_load(text) or {}
        else:
            data = json.loads(text)

        self.projects = data.get("projects", {})
        self._build_indices()

    def _build_indices(self):
        """Build reverse-lookup indices."""
        self._repo_index.clear()
        self._chat_index.clear()
        self._person_index.clear()

        for name, proj in self.projects.items():
            for repo in proj.get("repos", []):
                self._repo_index[repo] = name
            for chat_id in proj.get("teams_chats", []):
                self._chat_index[chat_id] = name
            for person in proj.get("people", []):
                self._person_index[person.lower()] = name

    def find_by_repo(self, repo: str) -> Optional[dict]:
        """Look up project by repo name (e.g., 'grobomo/hackathon26')."""
        name = self._repo_index.get(repo)
        if name:
            return {"name": name, **self.projects[name]}
        return None

    def find_by_chat(self, chat_id: str) -> Optional[dict]:
        """Look up project by Teams chat ID."""
        name = self._chat_index.get(chat_id)
        if name:
            return {"name": name, **self.projects[name]}
        return None

    def find_by_person(self, person: str) -> Optional[dict]:
        """Look up project by person name."""
        name = self._person_index.get(person.lower())
        if name:
            return {"name": name, **self.projects[name]}
        return None

    def find_by_channel(self, source: str, channel: str) -> Optional[dict]:
        """Look up project by source + channel."""
        if source == "github":
            return self.find_by_repo(channel)
        elif source == "teams":
            return self.find_by_chat(channel)
        return None

    def all_repos(self) -> list[str]:
        """Return all registered repos."""
        return list(self._repo_index.keys())

    def all_chat_ids(self) -> list[str]:
        """Return all registered Teams chat IDs."""
        return list(self._chat_index.keys())
