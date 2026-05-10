# My Skill That Does Data Processing Without Any Frontmatter

## Step 1: Read the Input

First, you should read the input data file carefully. Make a note of every field you observe and its type. This is important because the next step relies on understanding the schema.

## Step 2: Validate the Schema

Now validate the schema. Check every field against the expected types from the specification document. If any field does not match, log a warning and continue. If all fields match, proceed to the transformation step.

## Step 3: Transform the Data

Transform each record according to the rules defined in the specification. The transformation may include renaming fields, converting types, filtering rows, or applying calculations. Each transform should be idempotent — running it twice should produce the same result.

## Step 4: Write the Output

Write the transformed data to output.json. The output must include a metadata block with the transform timestamp and record count. Each record must include all required fields from the specification.

## Step 5: Validate the Output

After writing, validate the output file to ensure all expectations are met. Check file existence, field presence, data types, and value ranges.

## Step 6: Generate a Summary

Generate a brief summary of the transformation including: total records processed, records filtered, fields renamed, and any warnings encountered.

## Step 7: Clean Up

Remove any temporary files created during the processing. Do not remove the input file or the output file.

## Step 8: Report Results

Report the results to the user in a clear, concise format. Include the summary statistics and any notable findings.

## FAQ: Why Should I Use This Skill?

This skill provides a systematic approach to data processing that ensures quality and consistency. By following these steps, you can process data files with confidence knowing that every record is validated and transformed correctly. The built-in validation steps catch errors early, preventing downstream issues.

## FAQ: What Types of Data Does This Support?

This skill supports JSON data of any structure. It can handle flat objects, nested objects, arrays, and mixed types. The validation step is schema-aware and can detect structural inconsistencies.

## FAQ: How Fast Is This?

The processing speed depends on the data size and the transformation complexity. For typical files with fewer than 10000 records, processing takes less than 10 seconds. For larger files, processing may take proportionally longer.

## FAQ: Can I Customize the Rules?

Yes! You can customize the transformation rules by modifying the rule definitions in the configuration. Each rule specifies a source field, a target field, and an optional transformation function.

## FAQ: What If The Data Is Invalid?

If the data fails validation, the skill logs detailed error messages indicating which records failed and why. You can use these messages to fix the data and re-run the transformation.

## FAQ: Does This Work With CSV?

Currently, this skill only supports JSON input. CSV support is planned for a future release. In the meantime, you can convert your CSV files to JSON using a separate tool.

## FAQ: How Do I Install This?

Simply place the skill file in your skills directory. No additional installation steps are required. The skill is self-contained and uses only standard Python libraries.

## FAQ: What If I Encounter an Error?

Check the error log for details. Most errors are caused by malformed input data or permission issues. If the error persists, check that the input file exists and is readable.

## FAQ: Can I Chain Multiple Skills?

Yes! This skill can be used as part of a pipeline. Its output format is compatible with downstream skills that expect JSON input. Simply configure the pipeline manifest to chain this skill with your other skills.

## FAQ: Is This Skill Production-Ready?

This skill has been tested with a variety of data formats and edge cases. It includes comprehensive error handling and validation. However, for production use, we recommend additional testing with your specific data patterns.

## FAQ: How Do I Contribute?

Contributions are welcome! Submit a pull request with your changes and a description of what you've added or fixed. Please include tests for any new functionality.

## FAQ: What Are The System Requirements?

Python 3.8 or higher is required. No additional libraries are needed. The skill uses only standard library modules.

## FAQ: How Do I Report A Bug?

Find a bug? Open an issue with a description of the problem and a minimal reproduction case. Include the input data and the expected output.

## FAQ: Can This Handle Binary Data?

No, this skill is designed for text-based JSON data only. Binary data should be converted to a text format before processing.

## FAQ: What's The Output Format?

The output is standard JSON with a metadata envelope. Each output file includes a timestamp, record count, and the transformed data in a structured format.

## FAQ: Does This Support Multilingual Data?

Yes, the skill uses UTF-8 encoding and supports multilingual text fields. Non-ASCII characters are handled correctly.

## FAQ: Can I Schedule This To Run Automatically?

The skill itself doesn't include scheduling, but you can integrate it with any task scheduler like cron or systemd timers. Write a wrapper script that invokes the skill on your schedule.

## FAQ: What About Data Privacy?

The skill processes all data locally. No data is sent to external services. All processing happens on your machine.

## FAQ: Can I Use This In A CI Pipeline?

Absolutely! The skill is designed for automated environments. Use the --ci flag for machine-readable output formats.

## FAQ: How Do I Get Started?

Start with a simple JSON file and run the default transformation. Check the output and adjust the rules as needed. The skill includes detailed documentation for each configuration option.

## FAQ: What If My JSON Is Malformed?

The skill includes a pre-validation step that checks JSON syntax before processing. Malformed JSON is rejected with a clear error message.

## FAQ: Can I Extend This Skill?

Yes, the skill is modular. You can add custom validation rules, transformation functions, and output formats. See the extension guide for details.

## FAQ: Where Can I Find Examples?

Example configurations and input files are available in the examples directory. These examples cover common use cases and edge cases.

## FAQ: How Do I Monitor Processing Progress?

The skill logs progress at each step. For large files, you can follow the log to see which records are being processed.

## FAQ: What's The Maximum File Size?

The skill can handle files up to 1GB. For files larger than this, consider splitting the data into smaller chunks. Processing large files may require additional memory.

## FAQ: Does This Support Streaming?

Not yet, but it's on the roadmap. For now, the skill loads the entire input file into memory before processing.

## FAQ: How Do I Handle Backwards Compatibility?

The output format is versioned. When new fields are added, old configurations continue to work. Breaking changes are documented in the changelog.

## FAQ: Can I Run This On Windows?

Yes, the skill is cross-platform. It has been tested on Windows, macOS, and Linux.

## FAQ: What's The License?

This skill is licensed under the MIT License. You are free to use, modify, and distribute it.

## FAQ: How Do I Get Support?

For support, open an issue on the project repository or contact the maintainers directly. We aim to respond within 24 hours.

## FAQ: How Do I Translate This Documentation?

The documentation is currently available in English only. Translations are welcome! Contact the maintainers if you'd like to contribute a translation.

## FAQ: What Are The Known Limitations?

- No CSV support (planned)
- No streaming support (planned)
- Memory-intensive for large files
- No distributed processing support

## FAQ: How Do I Rollback An Update?

To rollback, replace the updated skill file with the previous version. Note that output format changes between versions may require re-processing.

## FAQ: Can I Use This With Docker?

Yes! A Docker image is available for containerized deployments. See the Docker section in the installation guide.

## FAQ: How Often Are Updates Released?

Updates are released quarterly. Critical bug fixes are released as needed. Check the changelog for details.

## FAQ: What Monitoring Is Available?

The skill includes built-in logging with configurable log levels. You can integrate these logs with your monitoring system using standard log forwarding.

## FAQ: Can I Contribute Tests?

Yes! Test contributions are especially welcome. Submit tests for edge cases, bug fixes, or new features with your pull request.

## FAQ: What CI Platforms Are Supported?

The skill has been tested with GitHub Actions, GitLab CI, and Jenkins. It should work with any CI platform that supports Python.

## FAQ: How Do I Configure The Skill?

Configuration is done through a config file placed next to the skill. See the configuration reference for all available options.

(Note: This SKILL.md is intentionally over 500 lines to demonstrate the max_500_lines static analysis check failure.)
