## 0.7.11 (2026-03-09)


### Bug Fixes
* handle AccessDeniedException when user lacks GetFarm permission ([`7fdc64c`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/7fdc64cda2d436e6cc320034af8701e8490daab6))


## 0.7.10 (2026-01-19)


### Features
* **log**: add path mapping logging for $JOB and $POSE env variables (#328) ([`3f71e05`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/3f71e057ddf2c15f862054edb6663689be4eb4e6))

### Bug Fixes
* detection of locked HDAs didn't follow fetch nodes (#333) ([`b7897e1`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/b7897e1d6a0ebacd04da5e6764e9537013fcec14))
* aws credential error message on node creation is not actionable (#309) ([`5a702a9`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/5a702a97d163441530e664242e1f9528cd515b63))
* unhelpful error with multiple deadline cloud nodes in the network (#308) ([`ede0b1b`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/ede0b1bd228a6000d01dbc6a3b2467709efad55e))


## 0.7.9 (2025-09-15)


### Features
* Added Houdini 21.0. (#297) ([`fd1444e`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/fd1444efed0d058b8cd56f7d4931727083ffaf9d))

### Bug Fixes
* Scenes with wedge nodes fail to render ([`4b34f2f`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/4b34f2feecb1fe799ccac227b9db2c0b835a632b))

## 0.7.8 (2025-07-24)



### Bug Fixes
* System installs have no default path ([`42a3454`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/42a3454338ab3c77e783f84233951bb3ca9ab7ef))
* properly clean up env vars created by the installer ([`a283647`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/a28364709357c20f66de19366e11e01a76a70f5f))
* remove tmp install directory after installation (#266) ([`6dcfccd`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/6dcfccd721c94976799ba13a4ff38fcc1bf0b917))

## 0.7.7 (2025-05-26)


### Features
* add Redshift pathmapping support (#261) ([`79a7531`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/79a753170dea011b4ce71549af84b581e01a67b0))

### Bug Fixes
* `Path not available` errors due to Environment Variables not being pathmapped(#259) ([`ea16801`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/ea16801e1ccc4faf183d20b5fe8746a15aea259d))
* ROP geometry nodes not being evaluated (#257) ([`cd02e83`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/cd02e83cf4bf686cd2474c395fae76972ac6bb14))

## 0.7.6 (2025-04-14)


### Bug Fixes
* bug that caused unhandled exceptions during file parsing (#235) ([`c304bca`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/c304bca9ca54ed2ad3e15bf5536877b80c8988f7))
* job attachments tab showing unevaluated and evaluated entries for same file (#234) ([`8582f6e`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/8582f6e2269ac70aad8a773dce63cf7c979d99b0))
* **submitter**: do not stop finding job attachment files if a node's filename parm could not be evaluated (#225) ([`cfe4425`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/cfe442535a6e9e09f707393ef816aaeb4a712003))


## 0.7.5 (2025-01-13)


### Features
* **adaptor**: Add HYTHON_EXECUTABLE environment variable  (#202) ([`aed5d1c`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/aed5d1c69f16f2c061831f9f9323090eff676b58))

### Bug Fixes
* **installer**: fix installer system installs (#203) ([`bd591cd`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/bd591cd32af8b86c7222c9c4fe98af360d182a93))

## 0.7.4 (2024-11-21)


### Features
* installer component support for Houdini 20.0 and 20.5 (#192) ([`244bc71`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/244bc7130999f38e5d408b0e845939411b09d2a4))

### Bug Fixes
* detect ROP USD Render output directories (#190) ([`fbeb81d`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/fbeb81df89944f6164d58835fb72cee37a3019b4))
* add deadline cloud soho script (#189) ([`f88974f`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/f88974ff8b79fced74c145f53821b1542f75092e))

## 0.7.3 (2024-10-11)


### Features
* add 20.0 and 20.5 dev installation (#173) ([`81a9d73`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/81a9d7385f7f80a04919db2ec359a8490377f8d2))


## 0.7.2 (2024-06-27)



### Bug Fixes
* fail when mantra license error is seen (#164) ([`6012e2b`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/6012e2b2859abf9f08db702022e571e7d9a35d36))

## 0.7.1 (2024-06-19)


### Features
* capture files with time-based variables for job attachments (#162) ([`55d1a6b`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/55d1a6b9b991f4f648e1eaf135b3f15ea55e514f))


## 0.7.0 (2024-05-29)

### BREAKING CHANGES
* run simulations sequentially (#149) ([`084553c`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/084553c5ab74921e6a33715540066ebeca4ac8f4))

### Features
* add help text for various fields (#156) ([`a9154b9`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/a9154b9d19a358b9bed47f52e757a74c91eadd00))

### Bug Fixes
* parse files no longer removes manually added attachments (#153) ([`a0c0988`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/a0c0988b2e9321edf92e4559aa9df7ed08682e6e))
* add missing submission telemetry event (#158) ([`f1ec97d`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/f1ec97d16312fbb950308ae3b5d0a88b4e98b36e))

## 0.6.3 (2024-05-22)



### Bug Fixes
* apply farm ID and queue ID settings correctly (#151) ([`1f7a8f4`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/1f7a8f44bd357223247c928a3da9903d213d221c))
* improve error messages when farm or queue ID not defined (#148) ([`48ee027`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/48ee027b1e7a2b3a81735a0205ffcd97ac37305e))

## 0.6.2 (2024-05-08)



### Bug Fixes
* output directory detection (#144) ([`4042fb5`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/4042fb517862c650d206b89072cb80fee3fb7308))
* pass through log verbosity (#141) ([`24ff283`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/24ff2833e01e4ba85e5bffd255bc6fcaf0644e85))
* verify Deadline Cloud render node input exists before submitting (#137) ([`4154518`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/415451872c531b3f69849378d716c9c191c07b8c))

## 0.6.1 (2024-05-01)



### Bug Fixes
* Windows pathmapping rules (#135) ([`d24499d`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/d24499d503846b6c63c786aa1a4c80890108d03e))
* improve error message for expired credentials (#131) ([`c69ea0b`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/c69ea0bdb40190d286d4e36ccfefc5efb0f1fafd))
* handle directories, references, and unnecessary files (#132) ([`0a909b3`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/0a909b3485966eb177a9088fb5342dcc880db474))
* adaptor wheel override (#128) ([`f4260e5`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/f4260e5aff9005422d741421178d737a811ca662))

## 0.6.0 (2024-04-01)

### BREAKING CHANGES
* public release (#104) ([`4023c1b`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/4023c1b629d171b8d435d009c60fe5d85b75e9dc))


### Bug Fixes
* include the adaptor deps in the package (#99) ([`c3caa57`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/c3caa5766310bb49ac87190c24be049829609579))
* safely handle potential deps when not submitting step deps (#106) ([`99f9285`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/99f9285968dbbee1ddf946ddfa503192212b6bec))
* re-add step dependencies, limit deadline-cloud input nodes, fix single step renders (#105) ([`23e4173`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/23e4173e730cbe58e2e812b5653e3567685ab8b4))
* include deps with openjd adaptor package (#103) ([`24ffda6`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/24ffda6c280014584e72e3572e10ba62a10e1b63))
* incorrect package name in create adaptor script (#102) ([`87f36e3`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/87f36e3d9e1c10c86068b6d3ffee4b47091fc0b6))

## 0.5.3 (2024-03-27)



### Bug Fixes
* differentiate between required and optional init keys (#100) ([`c2dd76b`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/c2dd76b34be516c0781a370c97e2d353d0f9b7f5))

## 0.5.2 (2024-03-26)


### Features
* Additional renderers and Wedge support (#52) ([`cad0931`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/cad0931c60b1bc42117ed8d1c3233925ecd42e26))
* Adds telemetry events to submitter and adaptor (#89) ([`96e3c44`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/96e3c44e85bda47dfb59fae9580485e1592316c0))

### Bug Fixes
* include deadline-cloud in the adaptor packaging script (#97) ([`fc2cb3d`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/fc2cb3d619126fb9ca291ec090fa297b773fe558))
* throw error on out of bounds wedgenum (#96) ([`cc6aed9`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/cc6aed9a16a1a4152ed625738d351d20a5fbf885))

## 0.5.1 (2024-03-15)

### Chores
* update deps deadline-cloud 0.40 (#87) ([`92497cf`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/92497cf0f3d116249f0c126bbbe30902286dd0b1))

## 0.5.0 (2024-03-08)

### BREAKING CHANGES
* **deps**: update openjd-adaptor-runtime to 0.5 (#83) ([`d661bdf`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/d661bdfd993733fdf401a3ffe34c23ba7dc8ca19))


### Bug Fixes
* make 0 the min for failed tasks and retry limit (#79) ([`aeba186`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/aeba18620d7f3ac8ba4c177de547f6ace5856b9a))
* use proper rez syntax for RezPackages (#77) ([`f1cd928`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/f1cd9287e910d7e47adaff04b782312a36a561be))

## 0.4.0 (2024-02-21)

### BREAKING CHANGES
* Create a script to build adaptor package artifacts (#66) ([`d4f39a2`](https://github.com/aws-deadline/deadline-cloud-for-houdini/commit/d4f39a2e4bc959e5edb326d42c87e81bdfb6bfa4))



