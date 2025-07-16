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
from typing import List

import aiohttp
import aiometer
from playwright.async_api import Browser, async_playwright
from yarl import URL


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

    start_request_time: datetime
    start_completion_time: datetime
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
    full_url: URL
    expected_final_url: str
    extension_url: URL

    @classmethod
    def from_url(cls, full_url: URL) -> NBGitpullerURL:
        if not full_url.path.startswith("/hub/user-redirect/git-pull"):
            raise ValueError(f"Not a valid nbgitpuller URL: {full_url} does not start with `/hub/user-redirect/git-pull`")

        return NBGitpullerURL(
            full_url,
            full_url.query["urlpath"].rstrip("/"),
            full_url.with_path("git-pull")
        )

    def make_fullpath(self, server_url: URL, targetpath: str) -> URL:
        return (server_url / self.extension_url.path).with_query(self.extension_url.query).extend_query(
            {"targetpath": targetpath}
        )


async def load_nbgitpuller_url(
    browser: Browser,
    server: RunningServer,
    token: str,
    nbgitpuller_url: NBGitpullerURL,
    screenshot_name: str,
):
    print(f"visiting {server.server_url}")
    start_time = datetime.now()

    context = await browser.new_context(
        extra_http_headers={"Authorization": f"token {token}"}
    )
    page = await context.new_page()
    await page.goto(str(nbgitpuller_url.make_fullpath(server.server_url, secrets.token_hex(8))))
    await page.wait_for_url(nbgitpuller_url.expected_final_url, timeout=120 * 10 * 1000)
    await page.wait_for_load_state("networkidle")
    await page.screenshot(path=screenshot_name)
    duration = datetime.now() - start_time
    print(f"{server.server_url} completed test in {duration}")


async def start_named_server(
    session: aiohttp.ClientSession, server: Server, profile_options: dict[str, str] | None = None
) -> RunningServer | None:
    """
    Try to start a named server as defined

    """
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
        start_time = datetime.now()
        if resp.status == 202:
            # we are awaiting start, let's look for events
            print(f"server {server.servername} waiting to start")
            async with session.get(
                server_api_url / "progress", headers=headers
            ) as progress_resp:
                async for line in progress_resp.content:
                    if line.decode().strip() == "":
                        # Empty line, just continue
                        continue
                    progress_event = json.loads(line.decode().strip()[len("data: ") :])
                    events.append(progress_event)
                    if progress_event.get("ready") == True:
                        print(progress_event)
                        return RunningServer(
                            servername=server.servername,
                            username=server.username,
                            hub_access=server.hub_access,
                            start_request_time=start_time,
                            start_completion_time=datetime.now(),
                            startup_events=events,
                            server_url=URL(
                                server.hub_access.url / progress_event["url"][1:]
                            ),  # Trim leading slashG
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
    started_server = await start_named_server(session, server, profile_options)
    match started_server:
        case RunningServer():
            await load_nbgitpuller_url(
                browser,
                started_server,
                auth_token,
                nbgitpuller_url,
                server.servername + ".png",
            )
        case _:
            print("Server startup failed")


async def main():
    argparser = argparse.ArgumentParser()
    argparser.add_argument("hub_url", help="Full URL to the JupyterHub to test against")
    argparser.add_argument("server_prefix", help="Prefix used for named servers started in this run")
    argparser.add_argument("username", help="Name of the user")
    argparser.add_argument("servers_count", type=int, help="Number of servers to start")
    argparser.add_argument(
        "--max-concurrency",
        type=int,
        default=30,
        help="Max Numbers of Servers to start at the same time",
    )
    argparser.add_argument(
        '--profile-option',
        help="Additional profile option to specify when starting the server (of key=value form)",
        nargs="*"
    )
    # nbgitpuller_url = URL("git-pull?repo=https%3A%2F%2Fkernel.googlesource.com%2Fpub%2Fscm%2Flinux%2Fkernel%2Fgit%2Ftorvalds%2Flinux.git&urlpath=lab&branch=master")
    nbgitpuller_url = NBGitpullerURL.from_url(URL(
        "https://staging.aws.2i2c.cloud/hub/user-redirect/git-pull?repo=https%3A%2F%2Fgithub.com%2Fspara%2Fcloud-101-geolab&urlpath=lab%2Ftree%2Fcloud-101-geolab%2F&branch=main"
    ))

    args = argparser.parse_args()

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
