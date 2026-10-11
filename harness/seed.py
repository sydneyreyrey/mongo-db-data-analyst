"""Harness v1: a generic business analyst that knows nothing about the company yet."""

from harness.schema import Harness, Identity, Output, Policy

DEFAULT_FORM = (
    "A short summary, then findings ranked by importance. Each finding states the metric, segment, period, "
    "direction and size, why it matters, the pipeline that proves it, and a chart where it helps."
)

# Report formats written before this default existed. Anything else was set by a reader.
OLD_DEFAULT_FORMS = (
    "A short summary, then findings ranked by importance. Each finding states the metric, segment, period, "
    "direction and size, why it matters, the pipeline that proves it, and a chart.",
)


def has_custom_format(form: str) -> bool:
    return form.strip() not in (DEFAULT_FORM, *OLD_DEFAULT_FORMS)


def seed_harness(workspace_id: str) -> Harness:
    return Harness(
        workspace_id=workspace_id,
        identity=Identity(
            role=("Business analyst for this company. You explore its data on your own and report what matters."),
            audience="A business owner who reads the report and leaves comments.",
            values=[
                "Every number you state must come from a query you ran.",
                "Say what you do not know instead of guessing.",
                "Never modify data. Your access is read only.",
            ],
        ),
        policy=Policy(scope="Analyze only the company database you are connected to."),
        output=Output(form=DEFAULT_FORM),
    )
