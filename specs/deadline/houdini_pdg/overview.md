# Overview: cooking Houdini PDG networks on AWS Deadline Cloud

## TLDR

The Deadline Cloud PDG scheduler lets an artist cook a Houdini TOP network on a Deadline Cloud farm. Each cook becomes one Deadline Cloud job: every TOP node that runs a command becomes a step, and every work item becomes a task. PDG creates work items while it cooks, but Deadline Cloud needs every task when the job is submitted. The scheduler therefore waits until PDG has generated the graph, checks that the graph can run on the farm, and submits the whole job with every task suspended. As PDG hands it each work item, the scheduler stores the item in S3 and lets its task run once the steps it depends on have finished. Workers run the items in Houdini, and the scheduler reports each result back to PDG.

## Problem

Houdini artists describe multi-stage work, such as simulating, caching, rendering and encoding a video, as a PDG TOP network. Deadline Cloud cannot run a TOP network today, because PDG generates work items during the cook and hands them out one at a time, while a Deadline Cloud job must describe all of its tasks when it is submitted. Running a TOP network on Deadline Cloud therefore requires a scheduler on the artist's workstation that acts as an adapter between PDG and Deadline Cloud.

## Background

### PDG

- A **TOP network** is a graph of TOP nodes connected by wires. Each node generates **work items**, and PDG then cooks them.
- A work item is one unit of work, such as rendering one frame. Some nodes, such as Wedge, run nothing themselves and only create or group work items.
- A **scheduler** decides where work items run. PDG offers each work item to the scheduler once it is ready to run, and the scheduler reports back whether it succeeded.

### Deadline Cloud

- A **job** is represented by an OpenJD template, which defines the job's **steps**, the **tasks** in each step, and the dependencies between steps.
- A task can be **suspended**, in which case it does not run until it is set READY.
- **Job attachments** move files through S3. A task receives the job's input files and the outputs of the steps it depends on.

### How a TOP network maps to a job

| TOP network | Deadline Cloud job |
|---|---|
| A node that runs a command | A step |
| A node that runs nothing | Nothing. Dependencies pass through it. |
| A work item | A task |
| A wire | A step dependency |
| A node that reads a file another step writes | A step dependency, so the task receives the file |

## Block diagram

```mermaid
flowchart TB
  subgraph WS["Artist's workstation (Houdini)"]
    PDG["PDG"]
    SCENE["Houdini scene<br/>(the open .hip)"]
    ORCH["Cook orchestration"]
    GRAPH["Graph snapshot<br/>and validation"]
    SUB["Submitter"]
    FARM["Farm job tracking"]
    STORE["Task storage"]
    DIAG["Diagnostics"]
    DISK["Working directory<br/>on disk"]
  end
  subgraph AWS["AWS"]
    DC["Deadline Cloud"]
    S3["S3"]
  end
  WORKER["Worker:<br/>Houdini adaptor"]
  PDG -->|"start, offer item,<br/>tick, stop"| ORCH
  ORCH -->|"read the graph,<br/>report results"| PDG
  ORCH -->|"read the file paths<br/>each step uses"| SCENE
  ORCH -->|"check graph,<br/>get job shape"| GRAPH
  ORCH -->|"submit job"| SUB
  ORCH -->|"release task,<br/>poll results"| FARM
  ORCH -->|"store item,<br/>read outputs"| STORE
  ORCH -->|"record each step<br/>of the cook"| DIAG
  SUB -->|"create job"| DC
  FARM -->|"set tasks READY,<br/>read status"| DC
  STORE --> S3
  DIAG -->|"cook log and<br/>debug folder"| DISK
  DC -->|"run task"| WORKER
  WORKER -->|"read item,<br/>write outputs"| S3
```

## Sequence diagram

A successful cook, from the artist starting the cook to the cook ending.

```mermaid
sequenceDiagram
  participant P as PDG
  participant S as Scheduler
  participant St as S3
  participant F as Deadline Cloud
  participant W as Worker
  P->>S: start cook
  loop while PDG is still generating
    P->>S: offer a work item early
    S-->>P: deferred, so PDG offers it again later
  end
  P->>S: offer a work item, once every node has generated
  S->>P: inspect the graph, read the scene
  S->>S: build the job shape and validate it
  alt graph refused
    S-->>P: failed, with each refusal and its fix
  else graph valid
    S->>F: create the job, every task suspended
  end
  loop for every work item
    P->>S: offer work item
    S->>St: store the work item
    S->>S: release the task
    S-->>P: item started
  end
  loop on each tick
    S->>F: set released tasks READY once their upstream steps succeed
    S->>F: find finished tasks
    S-->>P: succeeded or failed, once per item
  end
  loop on the farm, for every READY task
    F->>W: run the task
    W->>St: download inputs and the work item
    W->>W: run the work item in Houdini
    W->>St: upload outputs
    W-->>F: succeeded or failed
  end
  P->>S: stop cook
  opt the artist stopped the cook
    S->>F: cancel the job
  end
```
