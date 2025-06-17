# jupyterhub-simulator

Newer performance simulator for testing workloads on a JupyterHub.

Successor to [hubtraf](https://github.com/yuvipanda/hubtraf).

## Design goals

- Test spawning a large number of users in a short period of time with some tuning knobs,
  to see how the infrastructure as a whole responds.
- Support all possible authentication types by simply using the JupyterHub API directly. We
  spawn multiple servers by using named server support in JupyterHub, something that was not
  yet implemented in the initial design time of hubtraf.
- Any user can run these tests, limited only by their hub's max number of per-user named servers.

## TODO

- Support spawning on hubs with profile lists.
- Support passing on to [playwright-python](https://playwright.dev/python/docs/intro) after the
  server has been started, so we can more accurately test user workflows directly.
- Output telemetry data in a fashion that can be easily graphed.