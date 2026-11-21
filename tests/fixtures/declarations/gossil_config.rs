//! Presentation settings consumed by the inherited terminal theme.
//!
//! Production configuration layering belongs to the command context. Callers
//! deserialize its effective colors into this view before resolving the theme.

/// Inherited color values, palettes and token overrides.
pub mod colors;

/// The color portion of effective application settings.
#[derive(
    Clone, Debug, Default, serde::Deserialize, getset::Getters, PartialEq, serde::Serialize,
)]
#[serde(default)]
pub struct Settings {
    /// Inherited color and style settings.
    ///
    /// # Returns
    ///
    /// A shared reference to the effective `colors` settings supplied by the caller.
    #[get = "pub"]
    colors: colors::Settings,
}
