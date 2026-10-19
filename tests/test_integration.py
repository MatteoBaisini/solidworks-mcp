"""Integration tests running against a live SolidWorks instance.

Skipped automatically when SolidWorks is offline or unreachable.
Run with: pytest -m solidworks
"""
import asyncio
import pytest

from solidworks_mcp import server


def is_solidworks_running() -> bool:
    """Checks if SolidWorks can be reached via COM without launching it."""
    try:
        import win32com.client
        app = win32com.client.GetActiveObject("SldWorks.Application")
        if app is not None and getattr(app, "RevisionNumber", None):
            return True
    except Exception:
        pass
    return False


@pytest.mark.solidworks
def test_plate_with_hole_volume_verification():
    """Builds a 100x50x10 plate with a Ø10 through hole and verifies volume == 49,214.6 mm³."""
    if not is_solidworks_running():
        pytest.skip("SolidWorks is not running or accessible via COM.")

    # 1. Create a new part
    res_part = asyncio.run(server.new_part())
    assert "New part created" in res_part

    try:
        # 2. Select top plane and create 100x50 centered rectangle sketch
        asyncio.run(server.select_plane("top"))
        asyncio.run(server.start_sketch())
        asyncio.run(server.sketch_rectangle(0.0, 0.0, 100.0, 50.0, centered=True))

        # 3. Extrude 10 mm
        res_ext = asyncio.run(server.extrude(depth_mm=10.0))
        assert "Extrusion" in res_ext

        # 4. Model check after extrude
        res_check1 = asyncio.run(server.check_model())
        assert "No rebuild errors" in res_check1

        # 5. Through hole Ø10 mm at the center
        asyncio.run(server.select_face_at_point([0.0, 0.0, 10.0]))
        res_hole = asyncio.run(server.hole(centers=[[0.0, 0.0, 10.0]], diameter_mm=10.0, through_all=True))
        assert "hole" in res_hole

        # 6. Model check after hole
        res_check2 = asyncio.run(server.check_model())
        assert "No rebuild errors" in res_check2

        # 7. Mass properties: volume of 100x50x10 plate with Ø10 hole:
        # 100 * 50 * 10 - pi * (5^2) * 10 = 50000 - 785.398 = 49214.60 mm^3
        mp = asyncio.run(server.get_mass_properties())
        assert isinstance(mp, dict)
        assert mp["volume_mm3"] == pytest.approx(49214.6, rel=1e-3)

    finally:
        # Clean up open test document
        try:
            def close_doc():
                sw = server.get_sw()
                doc = sw.ActiveDoc
                if doc:
                    title = doc.GetTitle()
                    sw.CloseDoc(title)
            asyncio.run(server.com_tool(close_doc))
        except Exception:
            pass
