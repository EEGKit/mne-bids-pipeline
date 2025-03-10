"""Noise covariance estimation.

Covariance matrices are computed and saved.
"""

from types import SimpleNamespace

import mne
from mne_bids import BIDSPath

from mne_bids_pipeline._config_import import _import_config
from mne_bids_pipeline._config_utils import (
    _bids_kwargs,
    _restrict_analyze_channels,
    get_eeg_reference,
    get_noise_cov_bids_path,
    get_subjects_sessions,
)
from mne_bids_pipeline._logging import gen_log_kwargs, logger
from mne_bids_pipeline._parallel import get_parallel_backend, parallel_func
from mne_bids_pipeline._report import _all_conditions, _open_report, _sanitize_cond_tag
from mne_bids_pipeline._run import (
    _prep_out_files,
    _sanitize_callable,
    _update_for_splits,
    failsafe_run,
    save_logs,
)
from mne_bids_pipeline.typing import InFilesT, OutFilesT


def get_input_fnames_cov(
    *,
    cfg: SimpleNamespace,
    subject: str,
    session: str | None,
) -> InFilesT:
    cov_type = _get_cov_type(cfg)
    in_files = dict()
    fname_epochs = BIDSPath(
        subject=subject,
        session=session,
        task=cfg.task,
        acquisition=cfg.acq,
        run=None,
        recording=cfg.rec,
        space=cfg.space,
        extension=".fif",
        suffix="epo",
        processing="clean",
        datatype=cfg.datatype,
        root=cfg.deriv_root,
        check=False,
    )
    in_files["report_info"] = fname_epochs.copy().update(processing="clean")
    _update_for_splits(in_files, "report_info", single=True)
    fname_evoked = fname_epochs.copy().update(
        suffix="ave", processing=None, check=False
    )
    if fname_evoked.fpath.exists():
        in_files["evoked"] = fname_evoked
    if cov_type == "custom":
        in_files["__unknown_inputs__"] = "custom noise_cov callable"
        return in_files
    if cov_type == "raw":
        bids_path_raw_noise = BIDSPath(
            subject=subject,
            session=session,
            task=cfg.task,
            acquisition=cfg.acq,
            run=None,
            recording=cfg.rec,
            space=cfg.space,
            processing="clean",
            suffix="raw",
            extension=".fif",
            datatype=cfg.datatype,
            root=cfg.deriv_root,
            check=False,
        )
        if cfg.noise_cov == "rest":
            bids_path_raw_noise.task = "rest"
        else:
            bids_path_raw_noise.task = "noise"
        in_files["raw"] = bids_path_raw_noise
    else:
        assert cov_type == "epochs", cov_type
        in_files["epochs"] = fname_epochs
        _update_for_splits(in_files, "epochs", single=True)
    return in_files


def compute_cov_from_epochs(
    *,
    tmin: float | None,
    tmax: float | None,
    cfg: SimpleNamespace,
    exec_params: SimpleNamespace,
    subject: str,
    session: str | None,
    in_files: InFilesT,
    out_files: InFilesT,
) -> mne.Covariance:
    epo_fname = in_files.pop("epochs")

    msg = "Computing regularized covariance based on epochs' baseline periods."
    logger.info(**gen_log_kwargs(message=msg))
    msg = f"Input:  {epo_fname.basename}"
    logger.info(**gen_log_kwargs(message=msg))
    msg = f"Output: {out_files['cov'].basename}"
    logger.info(**gen_log_kwargs(message=msg))

    epochs = mne.read_epochs(epo_fname, preload=True)
    cov = mne.compute_covariance(
        epochs,
        tmin=tmin,
        tmax=tmax,
        method=cfg.noise_cov_method,
        rank="info",
        verbose="error",  # TODO: not baseline corrected, maybe problematic?
    )
    return cov


def compute_cov_from_raw(
    *,
    cfg: SimpleNamespace,
    exec_params: SimpleNamespace,
    subject: str,
    session: str | None,
    in_files: InFilesT,
    out_files: InFilesT,
) -> mne.Covariance:
    fname_raw = in_files.pop("raw")
    run_msg = "resting-state" if fname_raw.task == "rest" else "empty-room"
    msg = f"Computing regularized covariance based on {run_msg} recording."
    logger.info(**gen_log_kwargs(message=msg))
    msg = f"Input:  {fname_raw.basename}"
    logger.info(**gen_log_kwargs(message=msg))
    msg = f"Output: {out_files['cov'].basename}"
    logger.info(**gen_log_kwargs(message=msg))

    raw_noise = mne.io.read_raw_fif(fname_raw, preload=True)
    cov = mne.compute_raw_covariance(
        raw_noise,
        method=cfg.noise_cov_method,
        rank="info",
    )
    return cov


def retrieve_custom_cov(
    *,
    cfg: SimpleNamespace,
    exec_params: SimpleNamespace,
    subject: str,
    session: str | None,
    in_files: InFilesT,
    out_files: InFilesT,
) -> mne.Covariance:
    # This should be the only place we use config.noise_cov (rather than cfg.*
    # entries)
    config = _import_config(
        config_path=exec_params.config_path,
        check=False,
        log=False,
    )
    assert cfg.noise_cov == "custom"
    assert callable(config.noise_cov)
    assert in_files == {}, in_files  # unknown

    # ... so we construct the input file we need here
    evoked_bids_path = BIDSPath(
        subject=subject,
        session=session,
        task=cfg.task,
        acquisition=cfg.acq,
        run=None,
        processing="clean",
        recording=cfg.rec,
        space=cfg.space,
        suffix="ave",
        extension=".fif",
        datatype=cfg.datatype,
        root=cfg.deriv_root,
        check=False,
    )

    msg = "Retrieving noise covariance matrix from custom user-supplied function"
    logger.info(**gen_log_kwargs(message=msg))
    msg = f"Output: {out_files['cov'].basename}"
    logger.info(**gen_log_kwargs(message=msg))

    cov = config.noise_cov(evoked_bids_path)
    assert isinstance(cov, mne.Covariance)
    return cov


def _get_cov_type(cfg: SimpleNamespace) -> str:
    if cfg.noise_cov == "custom":
        return "custom"
    elif cfg.noise_cov == "rest":
        return "raw"
    elif cfg.noise_cov == "emptyroom" and "eeg" not in cfg.ch_types:
        return "raw"
    else:
        return "epochs"


@failsafe_run(
    get_input_fnames=get_input_fnames_cov,
)
def run_covariance(
    *,
    cfg: SimpleNamespace,
    exec_params: SimpleNamespace,
    subject: str,
    session: str | None = None,
    in_files: InFilesT,
) -> OutFilesT:
    import matplotlib.pyplot as plt

    out_files = dict()
    out_files["cov"] = get_noise_cov_bids_path(
        cfg=cfg, subject=subject, session=session
    )
    cov_type = _get_cov_type(cfg)
    fname_info = in_files.pop("report_info")
    fname_evoked = in_files.pop("evoked", None)
    if cov_type == "custom":
        cov = retrieve_custom_cov(
            cfg=cfg,
            subject=subject,
            session=session,
            in_files=in_files,
            out_files=out_files,
            exec_params=exec_params,
        )
    elif cov_type == "raw":
        cov = compute_cov_from_raw(
            cfg=cfg,
            subject=subject,
            session=session,
            in_files=in_files,
            out_files=out_files,
            exec_params=exec_params,
        )
    else:
        tmin, tmax = cfg.noise_cov
        cov = compute_cov_from_epochs(
            tmin=tmin,
            tmax=tmax,
            cfg=cfg,
            subject=subject,
            session=session,
            in_files=in_files,
            out_files=out_files,
            exec_params=exec_params,
        )
    cov.save(out_files["cov"], overwrite=True)

    # Report
    with _open_report(
        cfg=cfg, exec_params=exec_params, subject=subject, session=session
    ) as report:
        msg = "Rendering noise covariance matrix and corresponding SVD."
        logger.info(**gen_log_kwargs(message=msg))
        report.add_covariance(
            cov=cov,
            info=fname_info,
            title="Noise covariance",
            replace=True,
        )
        if fname_evoked is not None:
            msg = "Rendering whitened evoked data."
            logger.info(**gen_log_kwargs(message=msg))
            all_evoked = mne.read_evokeds(fname_evoked)
            conditions = _all_conditions(cfg=cfg)
            assert len(all_evoked) == len(conditions)
            section = "Noise covariance"
            for evoked, condition in zip(all_evoked, conditions):
                _restrict_analyze_channels(evoked, cfg)
                tags: tuple[str, ...] = (
                    "evoked",
                    "covariance",
                    _sanitize_cond_tag(condition),
                )
                title = f"Whitening: {condition}"
                if condition not in cfg.conditions:
                    tags = tags + ("contrast",)
                fig = evoked.plot_white(cov, verbose="error")
                report.add_figure(
                    fig=fig,
                    title=title,
                    tags=tags,
                    section=section,
                    replace=True,
                )
                plt.close(fig)

    assert len(in_files) == 0, in_files
    return _prep_out_files(exec_params=exec_params, out_files=out_files)


def get_config(
    *,
    config: SimpleNamespace,
) -> SimpleNamespace:
    cfg = SimpleNamespace(
        spatial_filter=config.spatial_filter,
        ch_types=config.ch_types,
        run_source_estimation=config.run_source_estimation,
        noise_cov=_sanitize_callable(config.noise_cov),
        conditions=config.conditions,
        contrasts=config.contrasts,
        analyze_channels=config.analyze_channels,
        eeg_reference=get_eeg_reference(config),
        noise_cov_method=config.noise_cov_method,
        **_bids_kwargs(config=config),
    )
    return cfg


def main(*, config: SimpleNamespace) -> None:
    """Run cov."""
    if not config.run_source_estimation:
        msg = "Skipping, run_source_estimation is set to False …"
        logger.info(**gen_log_kwargs(message=msg, emoji="skip"))
        return

    # Note that we're using config.noise_cov here and not adding it to
    # cfg, as in case it's a function, it won't work when running parallel jobs

    if config.noise_cov == "ad-hoc":
        msg = "Skipping, using ad-hoc diagonal covariance …"
        logger.info(**gen_log_kwargs(message=msg, emoji="skip"))
        return

    with get_parallel_backend(config.exec_params):
        parallel, run_func = parallel_func(
            run_covariance, exec_params=config.exec_params
        )
        logs = parallel(
            run_func(
                cfg=get_config(config=config),
                exec_params=config.exec_params,
                subject=subject,
                session=session,
            )
            for subject, sessions in get_subjects_sessions(config).items()
            for session in sessions
        )
    save_logs(config=config, logs=logs)
