import os
from contextlib import contextmanager
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Tuple,
    Union,
)

from dvc.exceptions import (
    CacheLinkError,
    DvcException,
    InvalidArgumentError,
    OutputDuplicationError,
    OutputNotFoundError,
    OverlappingOutputPathsError,
)
from dvc.repo.scm_context import scm_context
from dvc.types import StrOrBytesPath
from dvc.ui import ui
from dvc.utils import glob_targets, resolve_output, resolve_paths

from . import locked

if TYPE_CHECKING:
    from dvc.repo import Repo
    from dvc.stage import Stage


class StageInfo(NamedTuple):
    stage: "Stage"
    output_exists: bool


def find_targets(
    targets: Union[StrOrBytesPath, Iterator[StrOrBytesPath]], glob: bool = False
) -> List[str]:
    if isinstance(targets, (str, bytes, os.PathLike)):
        targets_list = [os.fsdecode(targets)]
    else:
        targets_list = [os.fsdecode(target) for target in targets]
    return glob_targets(targets_list, glob=glob)


def validate_args(targets: List[str], **kwargs: Any) -> None:
    invalid_opt = None
    to_remote = kwargs.get("to_remote")

    if len(targets) > 1 and kwargs.get("file"):
        raise InvalidArgumentError("cannot use '--file' with multiple targets")

    if to_remote or kwargs.get("out"):
        message = "{option} can't be used with "
        message += "--to-remote" if to_remote else "-o"
        if len(targets) != 1:
            invalid_opt = "multiple targets"
        elif kwargs.get("no_commit"):
            invalid_opt = "--no-commit option"
        elif kwargs.get("external"):
            invalid_opt = "--external option"
    else:
        message = "{option} can't be used without --to-remote"
        if kwargs.get("remote"):
            invalid_opt = "--remote"
        elif kwargs.get("jobs"):
            invalid_opt = "--remote-jobs"

    if invalid_opt is not None:
        raise InvalidArgumentError(message.format(option=invalid_opt))


def get_or_create_stage(
    repo: "Repo",
    target: str,
    file: Optional[str] = None,
    transfer: bool = False,
    external: bool = False,
    force: bool = False,
    out: Optional[str] = None,
) -> StageInfo:
    if out:
        target = resolve_output(target, out, force=force)
    path, wdir, out = resolve_paths(repo, target, always_local=transfer and not out)

    try:
        (out_obj,) = repo.find_outs_by_path(target, strict=False)
        stage = out_obj.stage
        if not stage.is_data_source:
            raise DvcException(f"cannot update {out!r}: not a data source")
        return StageInfo(stage, output_exists=True)
    except OutputNotFoundError:
        stage = repo.stage.create(
            single_stage=True,
            validate=False,
            fname=file or path,
            wdir=wdir,
            outs=[out],
            external=external,
            force=force,
        )
        return StageInfo(stage, output_exists=False)


OVERLAPPING_CHILD_FMT = (
    "Cannot add '{out}', because it is overlapping with other "
    "DVC tracked output: '{parent}'.\n"
    "To include '{out}' in '{parent}', run "
    "'dvc commit {parent_stage}'"
)

OVERLAPPING_PARENT_FMT = (
    "Cannot add '{parent}', because it is overlapping with other "
    "DVC tracked output: '{out}'.\n"
    "To include '{out}' in '{parent}', run "
    "'dvc remove {out_stage}' and then 'dvc add {parent}'"
)


@contextmanager
def translate_graph_error(stages: List["Stage"]) -> Iterator[None]:
    try:
        yield
    except OverlappingOutputPathsError as exc:
        if exc.parent in [o for s in stages for o in s.outs]:
            msg = OVERLAPPING_PARENT_FMT.format(
                out=exc.overlapping_out,
                parent=exc.parent,
                out_stage=exc.overlapping_out.stage.addressing,
            )
        else:
            msg = OVERLAPPING_CHILD_FMT.format(
                out=exc.overlapping_out,
                parent=exc.parent,
                parent_stage=exc.parent.stage.addressing,
            )
        raise OverlappingOutputPathsError(  # noqa: B904
            exc.parent, exc.overlapping_out, msg
        )
    except OutputDuplicationError as exc:
        raise OutputDuplicationError(  # noqa: B904
            exc.output, list(set(exc.stages) - set(stages))
        )


def progress_iter(stages: Dict[str, StageInfo]) -> Iterator[Tuple[str, StageInfo]]:
    total = len(stages)
    desc = "Adding..."
    with ui.progress(
        stages.items(), total=total, desc=desc, unit="file", leave=True
    ) as pbar:
        if total == 1:
            pbar.bar_format = desc
            pbar.refresh()

        for item, stage_info in pbar:
            if total > 1:
                pbar.set_msg(str(stage_info.stage.outs[0]))
                pbar.refresh()
            yield item, stage_info
            if total == 1:  # restore bar format for stats
                # pylint: disable=no-member
                pbar.bar_format = pbar.BAR_FMT_DEFAULT


LINK_FAILURE_MESSAGE = (
    "\nSome targets could not be linked from cache to workspace.\n{}\n"
    "To re-link these targets, reconfigure cache types and then run:\n"
    "\n\tdvc checkout {}"
)


@contextmanager
def warn_link_failures() -> Iterator[List[str]]:
    link_failures: List[str] = []
    try:
        yield link_failures
    finally:
        if link_failures:
            msg = LINK_FAILURE_MESSAGE.format(
                CacheLinkError.SUPPORT_LINK,
                " ".join(link_failures),
            )
            ui.error_write(msg)


def _add_transfer(
    stage: "Stage",
    source: str,
    remote: Optional[str] = None,
    to_remote: bool = False,
    jobs: Optional[int] = None,
    force: bool = False,
) -> None:
    odb = None
    if to_remote:
        odb = stage.repo.cloud.get_remote_odb(remote, "add")
    stage.transfer(source, odb=odb, to_remote=to_remote, jobs=jobs, force=force)
    stage.dump()


def _add(stage: "Stage", source: Optional[str] = None, no_commit: bool = False) -> None:
    out = stage.outs[0]
    path = out.fs.path.abspath(source) if source else None
    try:
        stage.add_outs(path, no_commit=no_commit)
    except CacheLinkError:
        stage.dump()
        raise
    stage.dump()


@locked
@scm_context
def add(  # noqa: PLR0913
    repo: "Repo",
    targets: Union[StrOrBytesPath, Iterator[StrOrBytesPath]],
    no_commit: bool = False,
    file: Optional[str] = None,
    external: bool = False,
    glob: bool = False,
    out: Optional[str] = None,
    remote: Optional[str] = None,
    to_remote: bool = False,
    jobs: Optional[int] = None,
    force: bool = False,
) -> List["Stage"]:
    transfer = to_remote or bool(out)
    add_targets = find_targets(targets, glob=glob)
    if not add_targets:
        return []

    validate_args(
        add_targets,
        no_commit=no_commit,
        file=file,
        external=external,
        out=out,
        remote=remote,
        to_remote=to_remote,
        jobs=jobs,
    )
    stages_with_targets = {
        target: get_or_create_stage(
            repo,
            target,
            file=file,
            transfer=transfer,
            external=external,
            force=force,
            out=out,
        )
        for target in add_targets
    }

    stages = [stage for stage, _ in stages_with_targets.values()]
    msg = "Collecting stages from the workspace"
    with translate_graph_error(stages), ui.status(msg) as st:
        repo.check_graph(stages=stages, callback=lambda: st.update("Checking graph"))

    if transfer:
        assert stages_with_targets
        (source, (stage, _)) = next(iter(stages_with_targets.items()))
        _add_transfer(stage, source, remote, to_remote, jobs=jobs, force=force)
        return [stage]

    with warn_link_failures() as link_failures:
        for source, (stage, output_exists) in progress_iter(stages_with_targets):
            try:
                _add(stage, source if output_exists else None, no_commit=no_commit)
            except CacheLinkError:
                link_failures.append(stage.relpath)
    return stages
