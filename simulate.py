from __future__ import annotations
import argparse
import asyncio
from enum import Enum
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime
from functools import partial
import time
from typing import List

import aiohttp
import aiometer
from playwright.async_api import Browser, async_playwright
from yarl import URL

@dataclass
class TimedResult[T]:
    duration: float
    result: T

@dataclass
class HubAccess:
    """
    Information needed to talk to a hub
    """

    url: URL
    token: str


@dataclass
class Server:
    """
    Represents a user server
    """

    servername: str
    username: str
    hub_access: HubAccess


@dataclass
class RunningServer(Server):
    """
    Represents a running user server
    """

    server_url: URL
    startup_events: List[dict]


@dataclass
class FailedServer(Server):
    """
    Represents a user server that failed to start
    """

    start_request_time: datetime
    start_failure_time: datetime
    startup_events: List[dict]

@dataclass
class NBGitpullerURL:
    repo: str
    ref: str
    open_path: str

    def make_fullpath(self, server_url: URL, targetpath: str) -> URL:
        query_params = {
            "repo": self.repo,
            "branch": self.ref,
            "targetPath": targetpath,
            "urlPath": os.path.join("tree", targetpath, self.open_path)
        }

        return (server_url / "git-pull").with_query(query_params)

    def make_expectedpath(self, server_url: URL, targetpath: str) -> URL:
        url_path = os.path.join("tree", targetpath, self.open_path)
        return server_url.joinpath(url_path, encoded=True)


async def load_nbgitpuller_url(
    browser: Browser,
    server: RunningServer,
    token: str,
    nbgitpuller_url: NBGitpullerURL,
    screenshot_name: str,
) -> TimedResult[None]:
    start_time = time.perf_counter()

    context = await browser.new_context(
        extra_http_headers={"Authorization": f"token {token}"}
    )
    page = await context.new_page()
    targetpath = secrets.token_hex(8)
    going_to = nbgitpuller_url.make_fullpath(server.server_url, targetpath)
    await page.goto(str(going_to))
    expected_final_full_url = str(nbgitpuller_url.make_expectedpath(server.server_url, targetpath))
    await page.wait_for_url(expected_final_full_url, timeout=120 * 10 * 1000)
    await page.wait_for_load_state("networkidle")
    await page.screenshot(path=screenshot_name, timeout=5 * 60 * 1000)
    return TimedResult(time.perf_counter() - start_time, None)

async def start_named_server(
    session: aiohttp.ClientSession, server: Server, profile_options: dict[str, str] | None = None,
) -> TimedResult[RunningServer | None]:
    """
    Try to start a named server as defined

    """
    start_time = time.perf_counter()
    headers = {"Authorization": f"token {server.hub_access.token}"}
    server_api_url = (
        server.hub_access.url
        / "hub/api/users"
        / server.username
        / "servers"
        / server.servername
    )
    events = []
    async with session.post(server_api_url, headers=headers, json=profile_options) as resp:
        if resp.status == 202:
            # we are awaiting start, let's look for events
            async with session.get(
                server_api_url / "progress", headers=headers, timeout=aiohttp.ClientTimeout(10 * 60)
            ) as progress_resp:
                async for line in progress_resp.content:
                    if line.decode().strip() == "":
                        # Empty line, just continue
                        continue
                    progress_event = json.loads(line.decode().strip()[len("data: ") :])
                    events.append(progress_event)
                    if progress_event.get("ready") == True:
                        return TimedResult(
                            time.perf_counter() - start_time,
                            RunningServer(
                                servername=server.servername,
                                username=server.username,
                                hub_access=server.hub_access,
                                startup_events=events,
                                server_url=URL(
                                    server.hub_access.url.joinpath(progress_event["url"].lstrip("/"), encoded=True)
                                ),
                            )
                        )
        elif resp.status == 201:
            # Means the server is immediately ready, and i don't want to deal with that yet
            raise NotImplementedError()
        else:
            # Some kinda error
            resp.raise_for_status()


async def payload(
    session: aiohttp.ClientSession,
    browser: Browser,
    auth_token: str,
    nbgitpuller_url: NBGitpullerURL,
    profile_options: dict[str, str] | None,
    server: Server,
):
    timing_info = {}
    print(f"Starting {server.servername}")
    start_server_result = await start_named_server(session, server, profile_options)
    match start_server_result:
        case TimedResult(server_startup_duration, started_server):
            timing_info["server_startup"] = server_startup_duration
            print(f"Started {server.servername} in {server_startup_duration:0.2f}s")
            match started_server:
                case RunningServer():
                    print(f"Starting nbgitpuller for {server.servername}")
                    result = await load_nbgitpuller_url(
                        browser,
                        started_server,
                        auth_token,
                        nbgitpuller_url,
                        server.servername + ".png",
                    )
                    timing_info["nbgitpuller"] = result.duration
                    print(f"nbgitpuller completed for {server.servername} in {result.duration:0.2f}s")
                case None:
                    print("Server startup failed")

    print(f"{server.servername}: {timing_info}")


async def main():
    argparser = argparse.ArgumentParser()
    argparser.add_argument("hub_url", help="Full URL to the JupyterHub to test against")
    argparser.add_argument("server_prefix", help="Prefix used for named servers started in this run")
    argparser.add_argument("username", help="Name of the user")
    argparser.add_argument("servers_count", type=int, help="Number of servers to start")
    # FIXME: This shouldn't be here.
    argparser.add_argument(
        "--max-concurrency",
        type=int,
        default=25,
        help="Max Numbers of Servers to start at the same time",
    )
    argparser.add_argument(
        '--profile-option',
        help="Additional profile option to specify when starting the server (of key=value form)",
        nargs="*"
    )

    args = argparser.parse_args()

    nbgitpuller_url = NBGitpullerURL("https://github.com/mspass-team/mspass_tutorial/", "master", "Earthscope2025")
    token = os.environ["JUPYTERHUB_TOKEN"]

    profile_options = None
    if args.profile_option:
        profile_options = {}
        for po in args.profile_option:
            key, value = po.split('=', 2)
            profile_options[key] = value

    hub_url = URL(args.hub_url)
    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=False)
        async with aiohttp.ClientSession() as session:
            servers_to_start = [
                Server(f"{args.server_prefix}-{i}", args.username, HubAccess(hub_url, token))
                for i in range(args.servers_count)
            ]
            await aiometer.run_all(
                [
                    partial(payload, session, browser, token, nbgitpuller_url, profile_options, server)
                    for server in servers_to_start
                ],
                max_at_once=args.max_concurrency,
            )

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
