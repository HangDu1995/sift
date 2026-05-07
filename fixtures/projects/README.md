# Built-in Test Projects

These Android projects are bundled with skill-eval for integration testing. They serve as real-world project fixtures that Skills can operate on during `batch --layers integration`.

## Usage

In `eval-config.yaml`, reference projects by relative path from the skill-eval package:

```yaml
pipelines:
  - name: "my-pipeline"
    project: "fixtures/projects/android-minimal"
    steps: [...]
```

Or use absolute paths for external projects:

```yaml
pipelines:
  - name: "real-project-test"
    project: "/path/to/real/android/project"
    steps: [...]
```

## Projects

### android-minimal

A bare-minimum Android project with a single Activity. Used for fast smoke tests.

- **Files**: ~12
- **SDK**: min 24, target 34
- **Content**: 1 Activity (MainActivity), 1 layout, strings
- **Best for**: Verifying a Skill can run without errors on a valid Android project

### android-medium

A more realistic Android project with multiple activities, networking layer, and data models.

- **Files**: ~20
- **SDK**: min 26, target 34
- **Content**: MainActivity + LoginActivity, HomeFragment, ApiClient (Retrofit), User model, multiple layouts
- **Dependencies**: Retrofit, OkHttp, Gson, ViewBinding, Lifecycle
- **Best for**: Testing Skills that interact with existing code (refactoring, review, conversion)
