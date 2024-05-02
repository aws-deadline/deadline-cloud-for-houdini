if exist ".\wheels" rmdir /s/q .\wheels
mkdir ".\wheels"

for %%g in (..\openjd-adaptor-runtime-for-python ..\deadline-cloud ..\deadline-cloud-for-houdini) do (
    echo "Building %%g..."
    cd %%g || exit /b 1
    hatch build || exit /b 1
    move dist\*.whl ..\deadline-cloud-for-houdini\wheels\
)
