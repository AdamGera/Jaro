use std::fmt;

#[derive(Debug)]
pub enum CliError {
    Missing(String),
    Database(String),
    Value(String),
    Runtime(String),
    DesktopUnavailable(String),
    Managed(String),
    NoSpeech,
    Usage(String),
}

impl CliError {
    pub fn missing(id: impl Into<String>) -> Self {
        Self::Missing(id.into())
    }

    pub fn value(msg: impl Into<String>) -> Self {
        Self::Value(msg.into())
    }

    pub fn runtime(msg: impl Into<String>) -> Self {
        Self::Runtime(msg.into())
    }

    pub fn desktop_unavailable(msg: impl Into<String>) -> Self {
        Self::DesktopUnavailable(msg.into())
    }

    pub fn managed(msg: impl Into<String>) -> Self {
        Self::Managed(msg.into())
    }

    pub fn usage(msg: impl Into<String>) -> Self {
        Self::Usage(msg.into())
    }

    pub fn formatted_for_subject(&self, subject: &str) -> String {
        match self {
            Self::Missing(_) => format!("{} not found.", capitalize(subject)),
            Self::Database(_) => format!("The saved {subject} library is unavailable."),
            Self::Value(m)
            | Self::Runtime(m)
            | Self::DesktopUnavailable(m)
            | Self::Managed(m)
            | Self::Usage(m) => m.clone(),
            Self::NoSpeech => "No speech was detected.".to_string(),
        }
    }
}

fn capitalize(s: &str) -> String {
    let mut chars = s.chars();
    match chars.next() {
        None => String::new(),
        Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
    }
}

impl fmt::Display for CliError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Missing(id) => write!(f, "Missing session: {id}"),
            Self::Database(m)
            | Self::Value(m)
            | Self::Runtime(m)
            | Self::DesktopUnavailable(m)
            | Self::Managed(m)
            | Self::Usage(m) => f.write_str(m),
            Self::NoSpeech => f.write_str("No speech was detected."),
        }
    }
}

impl std::error::Error for CliError {}

impl From<rusqlite::Error> for CliError {
    fn from(err: rusqlite::Error) -> Self {
        Self::Database(err.to_string())
    }
}

impl From<std::io::Error> for CliError {
    fn from(err: std::io::Error) -> Self {
        Self::Runtime(err.to_string())
    }
}
