import argparse
import asyncio
from functools import partial
import secrets
import time
import aiometer
from datetime import datetime
from typing import List, override
import aiohttp
import os
import json
from yarl import URL
from dataclasses import dataclass
from playwright.async_api import async_playwright, Browser


@dataclass
class ServerStartResult:
    servername: str
    username: str
    start_time: datetime
    completion_time: datetime
    started_successfully: bool
    events: List[dict]
    server_url: URL

    @property
    def startup_duration(self):
        return self.completion_time - self.start_time

    @override
    def __str__(self) -> str:
        return f"user:{self.username} server:{self.servername} started:{self.started_successfully} startup_duration:{self.startup_duration}"


async def load_nbgitpuller_url(browser: Browser, server_url: URL, token: str, nbgitpuller_url: URL, screenshot_name: str):
    print(f"visiting {server_url}")
    nbgitpuller_url = nbgitpuller_url.extend_query({
        'targetpath': secrets.token_urlsafe(8)
    })
    target_url = (server_url / nbgitpuller_url.path).with_query(nbgitpuller_url.query)
    await_url = server_url / target_url.query.get("urlpath", "/lab").rstrip("/")
    start_time = datetime.now()

    context = await browser.new_context(extra_http_headers={
        "Authorization": f"token {token}"
    })
    page = await context.new_page()
    await page.goto(str(target_url))
    await page.wait_for_url(str(await_url), timeout=120 * 10 * 1000)
    await page.wait_for_load_state("networkidle")
    await page.screenshot(path=screenshot_name)
    duration = datetime.now() - start_time
    print(f"{server_url} completed test in {duration}")



async def start_named_server(session: aiohttp.ClientSession, hub_url: URL, username: str, server_name: str) -> ServerStartResult:
    """
    Try to start a named server as defined

    """
    server_api_url = hub_url / "hub/api/users" / username / "servers" / server_name
    events = []
    async with session.post(server_api_url) as resp:
        start_time = datetime.now()
        if resp.status == 202:
            # we are awaiting start, let's look for events
            print(f"server {server_name} waiting to start")
            async with session.get(server_api_url / "progress") as progress_resp:
                async for line in progress_resp.content:
                    if line.decode().strip() == '':
                        # Empty line, just continue
                        continue
                    progress_event = json.loads(line.decode().strip()[len("data: "):])
                    events.append(progress_event)
                    if progress_event.get("ready") == True:
                        print(progress_event)
                        return ServerStartResult(
                            username=username,
                            servername=server_name,
                            start_time=start_time,
                            completion_time=datetime.now(),
                            started_successfully=True,
                            events=events,
                            server_url=URL(hub_url / progress_event['url'][1:]) # Trim leading slashG
                        )
        elif resp.status == 201:
            # Means the server is immediately ready, and i don't want to deal with that yet
            raise NotImplementedError()
        else:
            # Some kinda error
            resp.raise_for_status()


async def main():
    argparser = argparse.ArgumentParser()
    argparser.add_argument("hub_url", help="Full URL to the JupyterHub to test against")
    argparser.add_argument("username", help="Name of the user")
    argparser.add_argument("servers_count", type=int, help="Number of servers to start")
    argparser.add_argument("--max-concurrency", type=int, default=30, help="Max Numbers of Servers to start at the same time")
    # nbgitpuller_url = URL("git-pull?repo=https%3A%2F%2Fkernel.googlesource.com%2Fpub%2Fscm%2Flinux%2Fkernel%2Fgit%2Ftorvalds%2Flinux.git&urlpath=lab&branch=master")
    nbgitpuller_url = URL("git-pull?repo=https%3A%2F%2Fgithub.com%2Fspara%2Fcloud-101-geolab&urlpath=lab%2Ftree%2Fcloud-101-geolab%2F&branch=main")

    args = argparser.parse_args()

    token = os.environ["JUPYTERHUB_TOKEN"]

    hub_url = URL(args.hub_url)
    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=False)
        async with aiohttp.ClientSession(headers={
            "Authorization": f"token {token}"
        }) as session:
            servernames = [f"perf-test-{i}" for i in range (args.servers_count)]
            all_servers = []
            async with aiometer.amap(partial(start_named_server, session, hub_url, args.username), servernames, max_at_once=args.max_concurrency) as servers:
                async for server in servers:
                    all_servers.append(server)

            await aiometer.run_all(
                [partial(load_nbgitpuller_url, browser, server.server_url, token, nbgitpuller_url, server.servername + ".png") for server in all_servers],
                max_at_once=args.max_concurrency
            )

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())