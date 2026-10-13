"""Router registry. Add a router: write a Router subclass and list it in ROUTERS."""

from boardsmith.routers.astar import AStar
from boardsmith.routers.base import RouteJob, RouteResult, Router, copy_board
from boardsmith.routers.freerouting import Freerouting

ROUTERS = {r.name: r for r in (Freerouting, AStar)}


def get(name):
    try:
        return ROUTERS[name]()
    except KeyError:
        raise ValueError(f"unknown router {name!r}; known: {sorted(ROUTERS)}") from None


__all__ = ["ROUTERS", "RouteJob", "RouteResult", "Router", "copy_board", "get"]
