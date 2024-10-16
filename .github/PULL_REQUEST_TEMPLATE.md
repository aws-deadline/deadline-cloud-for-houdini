*delete text starting here*
Please ensure that you have read through the [contributing guidelines](https://github.com/aws-deadline/deadline-cloud-for-houdini/blob/mainline/CONTRIBUTING.md#contributing-via-pull-requests) before continuing.
*delete text ending here*

Fixes: *<insert link to GitHub issue here>*

### What was the problem/requirement? (What/Why)

### What was the solution? (How)

### What is the impact of this change?

### How was this change tested?

*delete text starting here*
See [DEVELOPMENT.md](https://github.com/aws-deadline/deadline-cloud-for-houdini/blob/mainline/DEVELOPMENT.md) for information on running tests.

- Have you run the unit tests?
*delete text ending here*

### Did you run the "Job Bundle Output Tests"? If not, why not? If so, paste the test results here.

*delete text starting here*
[Placeholder] See the "Integration Tests" subsection of the
[Running Submitter Tests](https://github.com/aws-deadline/deadline-cloud-for-houdini/blob/mainline/DEVELOPMENT.md#running-submitter-tests)
section of DEVELOPMENT.md for information on these tests.

```
Required: paste the contents of job_bundle_output_tests/test-job-bundle-results.txt here
```
*delete text ending here*

### Was this change documented?

*delete text starting here*
- Did you update all relevant docstrings for Python functions that you modified?
- Should the README.md, DEVELOPMENT.md, or other documents in the repository's docs/ directory be updated along with your change?
- Should the schema files for the adaptor's init-data or run-data be updated?
*delete text ending here*

### Is this a breaking change?

*delete text starting here*
A breaking change is one that modifies a public contract in some way or otherwise changes functionality of this application in a way
that is not backwards compatible. Examples of changes that are breaking include:

1. Adding a new required value to the init-data or run-data of the adaptor;
2. Deleting or renaming a value in the init-data or run-data of the adaptor; and
3. Otherwise modifying the interface of the adaptor such that a job submitted with an older version of the Houdini submitter plug-in
   will not work with a version of the adaptor that includes your modification.

If so, then please describe the changes that users of this package must make to update their scripts, or Python applications. Also,
please ensure that the title of your commit follows our conventional commit guidelines in 
[CONTRIBUTING.md](https://github.com/aws-deadline/deadline-cloud-for-houdini/blob/mainline/CONTRIBUTING.md#conventional-commits) for breaking changes.
*delete text ending here*

----

*By submitting this pull request, I confirm that you can use, modify, copy, and redistribute this contribution, under the terms of your choice.*
