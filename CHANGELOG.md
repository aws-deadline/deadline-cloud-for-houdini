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



