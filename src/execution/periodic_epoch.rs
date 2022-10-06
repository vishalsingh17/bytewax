use std::fmt::Debug;
use std::task::Poll;
use std::time::{Duration, Instant};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::PyResult;
use timely::dataflow::operators::generic::builder_rc::OperatorBuilder;
use timely::dataflow::ProbeHandle;
use timely::dataflow::Scope;
use timely::dataflow::Stream;
use timely::Data;

use crate::inputs::InputReader;
use crate::recovery::{State, StateOp, StepId};
use crate::recovery::{StateKey, StateUpdate};
use crate::recovery::{StateRecoveryKey, StateUpdateStream};

use super::EpochConfig;

/// Increment epochs at regular system time intervals.
///
/// This is the default with 10 second epoch intervals if no
/// `epoch_config` is passed to your execution entry point.
///
/// Args:
///
///   epoch_length (datetime.timedelta): System time length of each
///       epoch.
///
/// Returns:
///
///   Config object. Pass this as the `epoch_config` parameter of
///   your execution entry point.
#[pyclass(module="bytewax.window", extends=EpochConfig)]
#[pyo3(text_signature = "(epoch_length)")]
pub(crate) struct PeriodicEpochConfig {
    #[pyo3(get)]
    pub(crate) epoch_length: chrono::Duration,
}

#[pymethods]
impl PeriodicEpochConfig {
    #[new]
    #[args(epoch_length)]
    pub(crate) fn new(epoch_length: chrono::Duration) -> (Self, EpochConfig) {
        (Self { epoch_length }, EpochConfig {})
    }

    /// Pickle as a tuple.
    fn __getstate__(&self) -> (&str, chrono::Duration) {
        ("PeriodicEpochConfig", self.epoch_length)
    }

    /// Egregious hack see [`SqliteRecoveryConfig::__getnewargs__`].
    fn __getnewargs__(&self) -> (chrono::Duration,) {
        (chrono::Duration::zero(),)
    }

    /// Unpickle from tuple of arguments.
    fn __setstate__(&mut self, state: &PyAny) -> PyResult<()> {
        if let Ok(("PeriodicEpochConfig", epoch_length)) = state.extract() {
            self.epoch_length = epoch_length;
            Ok(())
        } else {
            Err(PyValueError::new_err(format!(
                "bad pickle contents for PeriodicEpochConfig: {state:?}"
            )))
        }
    }
}

/// Input source that increments the epoch periodically by system
/// time.
pub(crate) fn periodic_epoch_source<S, D>(
    scope: &S,
    step_id: StepId,
    state_key: StateKey,
    mut reader: Box<dyn InputReader<D>>,
    start_at: S::Timestamp,
    probe: &ProbeHandle<S::Timestamp>,
    epoch_length: Duration,
) -> (Stream<S, D>, StateUpdateStream<S>)
where
    S: Scope<Timestamp = u64>,
    D: Data + Debug,
{
    let mut op_builder = OperatorBuilder::new(format!("{step_id}"), scope.clone());

    let (mut output_wrapper, output_stream) = op_builder.new_output();
    let (mut state_update_wrapper, state_update_stream) = op_builder.new_output();

    let probe = probe.clone();
    let info = op_builder.operator_info();
    let activator = scope.activator_for(&info.address[..]);

    op_builder.build(move |mut init_caps| {
        let mut state_update_cap = init_caps.pop().map(|cap| cap.delayed(&start_at));
        let mut output_cap = init_caps.pop().map(|cap| cap.delayed(&start_at));

        let mut eof = false;
        let mut epoch_started = Instant::now();

        move |_input_frontiers| {
            if let (Some(output_cap), Some(state_update_cap)) =
                (output_cap.as_mut(), state_update_cap.as_mut())
            {
                assert!(output_cap.time() == state_update_cap.time());
                let epoch = output_cap.time();

                if !probe.less_than(epoch) {
                    if epoch_started.elapsed() > epoch_length {
                        // Snapshot just before incrementing epoch to
                        // get the "end of the epoch state".
                        let snapshot = reader.snapshot();
                        let recovery_key = StateRecoveryKey {
                            step_id: step_id.clone(),
                            state_key: state_key.clone(),
                            epoch: epoch.clone(),
                        };
                        let op = StateOp::Upsert(State {
                            snapshot,
                            next_awake: None,
                        });
                        let update = StateUpdate(recovery_key, op);
                        state_update_wrapper
                            .activate()
                            .session(&state_update_cap)
                            .give(update);

                        let next_epoch = epoch + 1;

                        output_cap.downgrade(&next_epoch);
                        state_update_cap.downgrade(&next_epoch);

                        epoch_started = Instant::now();
                    }

                    match reader.next() {
                        Poll::Pending => {}
                        Poll::Ready(None) => {
                            eof = true;
                        }
                        Poll::Ready(Some(item)) => {
                            output_wrapper.activate().session(&output_cap).give(item);
                        }
                    }
                }
            }

            if eof {
                output_cap = None;
                state_update_cap = None;
            } else {
                activator.activate();
            }
        }
    });

    (output_stream, state_update_stream)
}
