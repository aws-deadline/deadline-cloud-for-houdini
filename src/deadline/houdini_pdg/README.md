# Houdini PDG scheduler for AWS Deadline Cloud (experimental)

This package is experimental and not functional yet. It is a PDG scheduler that will cook a TOP network as one AWS Deadline Cloud job. In this build, starting a cook fails. Do not use it in production scenes.

## Registering the scheduler by hand

The installer does not register the scheduler, so it does not appear in anyone's TOP menu by default. To try it in a development checkout:

1. Run the dev install from the repository root:

   ```sh
   hatch run install
   ```

2. Create a Houdini package file, for example `deadline_pdg_for_houdini.json`, in your Houdini user preferences `packages/` directory. Replace `<repo>` with the absolute path of your checkout:

   ```json
   {
       "hpath": "<repo>/src/deadline/houdini_pdg/houdini_plugin",
       "env": [
           {"PYTHONPATH": {"value": "<repo>/src", "method": "append"}}
       ]
   }
   ```

   `hpath` makes Houdini find the scheduler type and its HDA. `PYTHONPATH` makes `deadline.houdini_pdg` importable inside Houdini.

3. Restart Houdini. In a TOP network, open the Tab menu and check that **Deadline Cloud Scheduler (Experimental)** appears under Schedulers.

To remove the scheduler, delete the package file and restart Houdini.

## Getting the results

The scheduler does not download outputs to your workstation. Steps hand files to each other on the farm. To fetch a cook's results, use Deadline Cloud Monitor or `deadline job download-output`.

## Layout

| Module | What it does |
|---|---|
| `graph.py` | Maps a TOP network to the shape of one job |
| `scene_reads.py` | Finds the files each step's scene reads, and which step writes them |
| `validation.py` | Refuses graphs the scheduler cannot cook, before anything is uploaded |
| `submitter.py` | Builds and submits the job through the same path as the ROP submitter |
| `task_storage.py` | Stores work item payloads and output records in S3 |
| `farm_job.py` | Releases each task when its inputs exist, and polls for results |
| `cook.py` | Runs one cook, from PDG's start callback to its stop callback |
| `diagnostics.py` | Writes each cook's debug directory and log |
| `houdini/` | Reads PDG and Houdini objects |
| `houdini_plugin/` | The scheduler registration, and the HDA that gives the node its icon, help, and Tab menu entry. Added to `hpath` by hand |

The worker side lives in `src/deadline/houdini_adaptor/pdg_actions.py`.

Only `host.py` and the registration module import `hou` or `pdg`. Everything else imports without Houdini, so the unit tests run anywhere.
