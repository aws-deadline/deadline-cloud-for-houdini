## How can I fail Mantra renders immediately if textures are missing?

There is a hidden feature in Mantra that can be used to fail the render immediately if textures are missing. To activate it:
1. Right click the Mantra node and go to "Parameters and channels" > "Edit Parameter interface"
2. In the "Create Parameters" section, select "Render Properties" and filter for "vm_abort_missing_texture"
3. Right click "Abort on missing texture (vm_abort_missing_texture)" and click "Install Parameter"
4. Click "Accept"
5. Left click the Mantra node and navigate to "Rendering" > "Render" in the parameter editor
6. Select the "Abort on missing texture" checkbox
