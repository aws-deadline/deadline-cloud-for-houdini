#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
set -xeou pipefail

APP=houdini
ADAPTOR_NAME=deadline-cloud-for-$APP

# This script generates an tar.gz artifact from $ADAPTOR_NAME and its dependencies
# that can be used to create a package for running the adaptor.

SCRIPTDIR=$(realpath $(dirname $0))

SOURCE=0
# Python 3.11 is for https://vfxplatform.com/ CY2024
PYTHON_VERSION=3.11
CONDA_PLATFORM=linux-64
TAR_BASE=

while [ $# -gt 0 ]; do
    case "${1}" in
    --source) SOURCE=1 ; shift ;;
    --platform) CONDA_PLATFORM="$2" ; shift 2 ;;
    --python) PYTHON_VERSION="$2" ; shift 2 ;;
    --tar-base) TAR_BASE="$2" ; shift 2 ;;
    *) echo "Unexpected option: $1"; exit 1 ;;
  esac
done

if [ "$CONDA_PLATFORM" = "linux-64" ]; then
    PYPI_PLATFORM=manylinux2014_x86_64
elif [ "$CONDA_PLATFORM" = "win-64" ]; then
    PYPI_PLATFORM=win_amd64
elif [ "$CONDA_PLATFORM" = "osx-64" ]; then
    PYPI_PLATFORM=macosx_10_9_x86_64
else
    echo "Unknown Conda operating system option --platform $CONDA_PLATFORM"
    exit 1
fi

if [ "$TAR_BASE" = "" ]; then
    TAR_BASE=$SCRIPTDIR/../$APP-openjd-py$PYTHON_VERSION-$CONDA_PLATFORM
fi

# Create a temporary prefix
WORKDIR=$(mktemp -d adaptor-pkg.XXXXXXXXXX)
function cleanup_workdir {
    echo "Cleaning up $WORKDIR"
    rm -rf $WORKDIR
}
trap cleanup_workdir EXIT

PREFIX=$WORKDIR/prefix

if [ "$CONDA_PLATFORM" = "win-64" ]; then
    BINDIR=$PREFIX/Library/bin
    PACKAGEDIR=$PREFIX/Library/opt/$ADAPTOR_NAME
else
    BINDIR=$PREFIX/bin
    PACKAGEDIR=$PREFIX/opt/$ADAPTOR_NAME
fi


mkdir -p $PREFIX
mkdir -p $PACKAGEDIR
mkdir -p $BINDIR

# Install the adaptor into the virtual env
if [ $SOURCE = 1 ]; then
    # In source mode, openjd-adaptor-runtime-for-python must be alongside this adaptor source
    RUNTIME_INSTALLABLE=$SCRIPTDIR/../../openjd-adaptor-runtime-for-python
    CLIENT_INSTALLABLE=$SCRIPTDIR/../../deadline-cloud
    ADAPTOR_INSTALLABLE=$SCRIPTDIR/..

    if [ "$CONDA_PLATFORM" = "win-64" ]; then
        DEPS="pyyaml jsonschema pywin32"
    else
        DEPS="pyyaml jsonschema"
    fi

    for DEP in $DEPS; do
        pip install \
            --target $PACKAGEDIR \
            --platform $PYPI_PLATFORM \
            --python-version $PYTHON_VERSION \
            --ignore-installed \
            --only-binary=:all: \
            $DEP
    done

    pip install \
        --target $PACKAGEDIR \
        --platform $PYPI_PLATFORM \
        --python-version $PYTHON_VERSION \
        --ignore-installed \
        --no-deps \
        $RUNTIME_INSTALLABLE

    # Install these two at the same time otherwise they overwrite eachother
    pip install \
        --target $PACKAGEDIR \
        --platform $PYPI_PLATFORM \
        --python-version $PYTHON_VERSION \
        --only-binary=:all: \
        --ignore-installed \
        $ADAPTOR_INSTALLABLE $CLIENT_INSTALLABLE
else
    # In PyPI mode, PyPI and/or a CodeArtifact must have these packages
    RUNTIME_INSTALLABLE=openjd-adaptor-runtime
    CLIENT_INSTALLABLE=deadline
    ADAPTOR_INSTALLABLE=$ADAPTOR_NAME

    pip install \
        --target $PACKAGEDIR \
        --platform $PYPI_PLATFORM \
        --python-version $PYTHON_VERSION \
        --ignore-installed \
        --no-deps \
        $RUNTIME_INSTALLABLE

    # Install these two at the same time otherwise they overwrite eachother
    pip install \
        --target $PACKAGEDIR \
        --platform $PYPI_PLATFORM \
        --python-version $PYTHON_VERSION \
        --ignore-installed \
        --only-binary=:all: \
        $ADAPTOR_INSTALLABLE $CLIENT_INSTALLABLE
fi


# Remove the submitter code
rm -r $PACKAGEDIR/deadline/*_submitter

# Remove the bin dir if there is one
if [ -d $PACKAGEDIR/bin ]; then
    rm -r $PACKAGEDIR/bin
fi

PYSCRIPT="from pathlib import Path
import sys
reentry_exe = Path(sys.argv[0]).absolute()
sys.path.append(str(reentry_exe.parent.parent / \"opt\" / \"$ADAPTOR_NAME\"))
from deadline.${APP}_adaptor.${APP^}Adaptor.__main__ import main
sys.exit(main(reentry_exe=reentry_exe))
"

cat <<EOF > $BINDIR/$APP-openjd
#!/usr/bin/env python3.11
$PYSCRIPT
EOF

# Temporary
cp $BINDIR/$APP-openjd $BINDIR/${APP^}Adaptor

chmod u+x $BINDIR/$APP-openjd $BINDIR/${APP^}Adaptor

if [ $CONDA_PLATFORM = "win-64" ]; then
    # Install setuptools to get cli-64.exe
    mkdir -p $WORKDIR/tmp
    pip install \
        --target $WORKDIR/tmp \
        --platform $PYPI_PLATFORM \
        --python-version $PYTHON_VERSION \
        --ignore-installed \
        --no-deps \
        setuptools

    # Use setuptools' cli-64.exe to define the entry point
    cat <<EOF > $BINDIR/$APP-openjd-script.py
#!C:\\Path\\To\\Python.exe
$PYSCRIPT
EOF
    cp $WORKDIR/tmp/setuptools/cli-64.exe $BINDIR/$APP-openjd.exe
fi

# Everything between the first "-" and the next "+" is the package version number
PACKAGEVER=$(cd $PACKAGEDIR; echo deadline_cloud_for*)
PACKAGEVER=${PACKAGEVER#*-}
PACKAGEVER=${PACKAGEVER%+*}
echo "Package version number is $PACKAGEVER"

# Create the tar artifact
GIT_TIMESTAMP="$(env TZ=UTC git log -1 --date=iso-strict-local --format="%ad")"
pushd $PREFIX
# See https://reproducible-builds.org/docs/archives/ for information about
# these options
#tar --mtime=$GIT_TIMESTAMP \
#     --sort=name \
#     --pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime \
#    --owner=0 --group=0 --numeric-owner \
#    -cf $TAR_BASE .
# TODO Switch to the above command once the build environment has tar version > 1.28
tar --owner=0 --group=0 --numeric-owner \
    -cf $TAR_BASE-$PACKAGEVER.tar.gz .
sha256sum $TAR_BASE-$PACKAGEVER.tar.gz
popd
