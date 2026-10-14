"""Quick end-to-end check: starts the server over stdio and talks to SolidWorks.

    python examples/smoke_test.py         -> read only (document info, feature tree, mass)
    python examples/smoke_test.py draw    -> also creates a 130x130x10 plate with a 50 mm hole

Extra arguments after "--" are passed to the server, e.g.
    python examples/smoke_test.py -- --config my.toml
"""
import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(draw: bool, server_args: list[str]) -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "solidworks_mcp", *server_args])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("TOOLS:", ", ".join(t.name for t in tools.tools), "\n")

            async def call(name, **args):
                result = await session.call_tool(name, args)
                for item in result.content:
                    print(f"[{name}]", getattr(item, "text", f"<{item.type}>"))

            await call("get_active_document_info")
            if draw:
                await call("new_part")
                await call("select_plane", plane="top")
                await call("start_sketch")
                await call("sketch_rectangle", width_mm=130, height_mm=130)
                await call("extrude", depth_mm=10)
                await call("select_face_at_point", x_mm=0, y_mm=10, z_mm=0)
                await call("start_sketch")
                await call("sketch_circle", diameter_mm=50)
                await call("cut_extrude", through_all=True)
                await call("get_bounding_box")
            await call("get_feature_tree")
            await call("get_mass_properties")


if __name__ == "__main__":
    argv = sys.argv[1:]
    extra = argv[argv.index("--") + 1:] if "--" in argv else []
    own = argv[:argv.index("--")] if "--" in argv else argv
    asyncio.run(main("draw" in own, extra))
