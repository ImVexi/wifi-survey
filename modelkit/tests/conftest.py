import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

FIXTURE = REPO / "tests" / "fixtures" / "sample_house.glb"


@pytest.fixture(scope="session")
def fixture_glb():
    if not FIXTURE.exists():
        from modelkit.synthetic import build_house

        build_house(FIXTURE)
    return FIXTURE


@pytest.fixture(scope="session")
def pipeline_out(tmp_path_factory, fixture_glb):
    from modelkit.pipeline import run

    out = tmp_path_factory.mktemp("pipeline")
    run(fixture_glb, out, name="sample_house")
    return out


@pytest.fixture(scope="session")
def work_dir(pipeline_out):
    return pipeline_out / "work"


@pytest.fixture(scope="session")
def metadata(pipeline_out):
    import json

    with open(pipeline_out / "metadata.json", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="session")
def floors_record(work_dir):
    import json

    with open(work_dir / "floors.json", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="session")
def walls_record(work_dir):
    import json

    with open(work_dir / "walls.json", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="session")
def regularized_record(work_dir):
    import json

    with open(work_dir / "regularized.json", encoding="utf-8") as handle:
        return json.load(handle)
