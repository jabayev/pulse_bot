"""Docker container status via the Engine API (unix socket).

Watches the prod compose project: critical container not running -> critical
alert; frequent restarts/exits -> restart-loop alert; recovery notice on Up.
"""

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from html import escape
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class ContainerInfo:
    name: str          # compose service name when available
    container_name: str
    state: str         # running / exited / restarting / created / paused / dead
    status: str        # human string: "Up 2 hours (healthy)"
    project: str = ""

    @property
    def is_running(self) -> bool:
        return self.state == "running"

    @property
    def is_healthy(self) -> Optional[bool]:
        if "(healthy)" in self.status:
            return True
        if "(unhealthy)" in self.status:
            return False
        return None


class DockerClient:
    def __init__(self, socket_path: str, project: str = "", timeout: float = 10.0):
        self.socket_path = socket_path
        self.project = project
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def list_containers(self) -> list[ContainerInfo]:
        connector = aiohttp.UnixConnector(path=self.socket_path)
        async with aiohttp.ClientSession(
            connector=connector, timeout=self.timeout
        ) as session:
            async with session.get(
                "http://docker/containers/json", params={"all": "1"}
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Docker API HTTP {resp.status}")
                payload = await resp.json(content_type=None)

        containers: list[ContainerInfo] = []
        for item in payload:
            labels = item.get("Labels") or {}
            project = labels.get("com.docker.compose.project", "")
            if self.project and project and project != self.project:
                continue
            raw_name = (item.get("Names") or ["?"])[0].lstrip("/")
            containers.append(
                ContainerInfo(
                    name=labels.get("com.docker.compose.service", raw_name),
                    container_name=raw_name,
                    state=str(item.get("State", "unknown")).lower(),
                    status=str(item.get("Status", "")),
                    project=project,
                )
            )
        containers.sort(key=lambda c: c.name)
        return containers


# --- Watcher ---------------------------------------------------------------

@dataclass
class ContainerEvent:
    kind: str  # "down" | "up" | "restart_loop" | "missing"
    name: str
    detail: str = ""


@dataclass
class _WatchState:
    down_since: Optional[float] = None
    alerted_down: bool = False
    alerted_loop: bool = False
    was_running: Optional[bool] = None
    exits: deque = field(default_factory=lambda: deque(maxlen=50))


class ContainerWatcher:
    def __init__(
        self,
        critical: list[str],
        down_seconds: int,
        restart_threshold: int,
        restart_window: int,
    ):
        self.critical = set(critical)
        self.down_seconds = down_seconds
        self.restart_threshold = restart_threshold
        self.restart_window = restart_window
        self._states: dict[str, _WatchState] = {}

    def evaluate(self, containers: list[ContainerInfo]) -> list[ContainerEvent]:
        now = time.monotonic()
        events: list[ContainerEvent] = []
        seen = {c.name: c for c in containers}
        # Match critical names against both the compose service and container name.
        for c in containers:
            if c.container_name in self.critical and c.name not in self.critical:
                seen[c.container_name] = c

        for name in sorted(self.critical):
            container = seen.get(name)
            state = self._states.setdefault(name, _WatchState())

            running = container is not None and container.is_running
            detail = container.status if container else "контейнер не найден"

            # Track exit/restart transitions for the loop detector.
            if state.was_running and not running:
                state.exits.append(now)
            state.was_running = running

            if container is not None and container.state == "restarting":
                state.exits.append(now)

            recent_exits = [t for t in state.exits if now - t <= self.restart_window]
            if len(recent_exits) >= self.restart_threshold and not state.alerted_loop:
                state.alerted_loop = True
                events.append(
                    ContainerEvent(
                        "restart_loop",
                        name,
                        f"{len(recent_exits)} рестартов за "
                        f"{self.restart_window // 60} мин · {detail}",
                    )
                )

            if running:
                if state.alerted_down:
                    events.append(ContainerEvent("up", name, detail))
                state.down_since = None
                state.alerted_down = False
                if not recent_exits:
                    state.alerted_loop = False
                continue

            if state.down_since is None:
                state.down_since = now
            elif (
                now - state.down_since >= self.down_seconds
                and not state.alerted_down
            ):
                state.alerted_down = True
                kind = "missing" if container is None else "down"
                events.append(ContainerEvent(kind, name, detail))

        return events


# --- Rendering -------------------------------------------------------------

_STATE_ICONS = {
    "running": "🟢",
    "restarting": "🟡",
    "exited": "🔴",
    "dead": "🔴",
    "paused": "⏸",
    "created": "⚪️",
}


def render_containers(containers: list[ContainerInfo], project: str) -> str:
    if not containers:
        return (
            f"❔ Контейнеры проекта <b>{escape(project or 'все')}</b> не найдены.\n"
            "Проверьте COMPOSE_PROJECT_NAME и доступ к /var/run/docker.sock."
        )

    running = sum(1 for c in containers if c.is_running)
    lines = [
        f"<b>🐳 Контейнеры · {escape(project or 'все проекты')}</b>\n",
        f"Запущено: <b>{running}</b> / {len(containers)}\n",
    ]
    for c in containers:
        icon = _STATE_ICONS.get(c.state, "❔")
        if c.is_healthy is False:
            icon = "🟠"
        lines.append(f"{icon} <b>{escape(c.name)}</b> — {escape(c.status or c.state)}")
    return "\n".join(lines)
