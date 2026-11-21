//! View-level UI components for full entity displays composed from atoms and molecules.

mod artifact_detail;
mod artifact_list;
mod iteration_detail;
mod iteration_graph;
mod iteration_list;
mod meta_get;
mod project_list;
mod project_show;
pub(super) mod search_result;
mod search_results;
mod task_detail;
mod task_list;

pub use artifact_detail::Component as ArtifactDetail;
pub use artifact_list::{ArtifactEntry, Component as ArtifactListView};
pub use iteration_detail::{Component as IterationDetail, TaskCounts};
pub use iteration_graph::{Component as IterationGraphView, GraphTask};
pub use iteration_list::{Component as IterationListView, IterationEntry};
pub use meta_get::Component as MetaGet;
pub use project_list::{Component as ProjectListView, ProjectEntry};
pub use project_show::Component as ProjectShow;
pub use search_results::Component as SearchResults;
pub use task_detail::Component as TaskDetail;
pub use task_list::{Component as TaskListView, TaskEntry};

/// List and graph output contracts.
#[cfg(test)]
mod coverage_controls;
