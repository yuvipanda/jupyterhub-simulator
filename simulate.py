import argparse
import asyncio
from functools import partial
import aiometer
from datetime import datetime
from typing import List, override
import aiohttp
import os
import json
from yarl import URL
from dataclasses import dataclass


@dataclass
class ServerStartResult:
    servername: str
    username: str
    start_time: datetime
    completion_time: datetime
    started_successfully: bool
    events: List[dict]

    @property
    def startup_duration(self):
        return self.completion_time - self.start_time

    @override
    def __str__(self) -> str:
        return f"user:{self.username} server:{self.servername} started:{self.started_successfully} startup_duration:{self.startup_duration}"

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
                        return ServerStartResult(
                            username=username,
                            servername=server_name,
                            start_time=start_time,
                            completion_time=datetime.now(),
                            started_successfully=True,
                            events=events
                        )
        elif resp.status == 201:
            # Means the server is immediately ready, and i don't want to deal with that yet
            raise NotImplementedError()


async def main():
    argparser = argparse.ArgumentParser()
    argparser.add_argument("hub_url", help="Full URL to the JupyterHub to test against")
    argparser.add_argument("username", help="Name of the user")
    argparser.add_argument("servers_count", type=int, help="Number of servers to start")
    argparser.add_argument("--max-concurrency", type=int, default=30, help="Max Numbers of Servers to start at the same time")

    args = argparser.parse_args()

    token = os.environ["JUPYTERHUB_TOKEN"]

    hub_url = URL(args.hub_url)
    async with aiohttp.ClientSession(headers={
        "Authorization": f"token {token}"
    }) as session:
        servernames = [f"perf-test-{i}" for i in range (args.servers_count)]
        async with aiometer.amap(partial(start_named_server, session, hub_url, args.username), servernames, max_at_once=args.max_concurrency) as servers:
            async for server in servers:
                print(server)

if __name__ == "__main__":
    asyncio.run(main())