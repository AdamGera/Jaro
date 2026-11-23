# Repository layout requirements

### Requirement 1 · Navigable source

**User Story:** As a contributor, I want to find the engine, desktop host, web interface, SDK, and tests without scanning individual source files at the repository root.

#### Acceptance Criteria

1. WHEN a contributor opens the repository, Python engine modules and their catalogs SHALL live together and Python unit tests SHALL have a dedicated directory.
2. WHEN a contributor reads the README, it SHALL link to the source layout, development checks, vulnerability-reporting guidance, and a roadmap that distinguishes current functionality from unverified work.

### Requirement 2 · Runtime and release compatibility

**User Story:** As an existing Jard user or developer, I want the new source layout to preserve the app, local library, and build workflows.

#### Acceptance Criteria

1. WHEN a user launches from Finder or starts the engine in a checkout, the existing launchers SHALL use the same built web UI, model cache, and library. The source CLI and SDK SHALL remain callable.
2. WHEN a maintainer builds the macOS bundle, the packaged helper entry points and resources SHALL retain their existing paths and the packager SHALL continue to include only explicitly listed runtime files.
3. WHEN a contributor runs verification from the repository root, the Python suite and web build SHALL be discoverable and runnable without recording audio or changing the installed app.
