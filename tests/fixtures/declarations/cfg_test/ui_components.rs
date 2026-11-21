//! Reusable UI components organized by granularity (atoms, molecules, views).

mod atoms;
mod molecules;
mod views;

#[cfg(test)]
pub use atoms::min_unique_prefix;
pub use atoms::{prefix_lengths_two_tier, unique_prefix_length_for_id, unique_prefix_lengths};
// The inherited summary component is available for a future `purge` command.
#[allow(unused_imports)]
pub use molecules::Summary;
pub use molecules::{Banner, ConfirmPrompt, ErrorMessage, FieldList, SuccessMessage, UpdateNotice};
pub use views::{
    ArtifactDetail, ArtifactEntry, ArtifactListView, GraphTask, IterationDetail, IterationEntry,
    IterationGraphView, IterationListView, MetaGet, ProjectEntry, ProjectListView, ProjectShow,
    SearchResults, TaskCounts, TaskDetail, TaskEntry, TaskListView,
};
