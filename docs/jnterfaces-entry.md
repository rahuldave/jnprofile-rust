---
title: The jnterfaces entry
---

jnterfaces has no entry for the rust profile in its `stack.json` yet, and its installer cannot read the `profile.json` of the profile. This page gives the entry and the change to the installer. The lab of this repository applies both to a copy of jnterfaces, and installs the profile with that copy. The change was made against jnterfaces at commit `2cfb176`.

## The entry in stack.json

Add this entry to the key `profiles` of `stack.json`, next to `python` and `typescript`:

```json
{
  "rust": {
    "package": "jnprofile-rust",
    "repository": "https://github.com/rahuldave/jnprofile-rust",
    "about": "a Rust project with Cargo",
    "detect": [
      "Cargo.toml"
    ]
  }
}
```

## The change to the installer

The installer of jnterfaces stops with "The profile.json of the rust profile is malformed" for two reasons:

- **The manifest.** `check_profile` accepts only `pyproject.toml` and `package.json` as the key `manifest`. The rust profile names `Cargo.toml`.
- **A tool with no packages.** `check_profile` needs at least one package for each tool. The tools of the rust profile are programs of the machine, so each tool has the empty list `[]`.

A third change follows from the first. `present_packages` reads the packages that the project file already has. It reads `Cargo.toml` as JSON, fails, and then the installer says "cannot read Cargo.toml". The change reads the table `[dev-dependencies]`, where `cargo add --dev` puts a crate. A tool with no packages also skips that read, so a `Cargo.toml` that does not parse does not stop the install, and the installer says "lint for rust needs no packages". The file `lab/jnterfaces-rust.patch` holds the change:

```diff
--- a/scripts/install_stack.py
+++ b/scripts/install_stack.py
@@ -169,7 +169,7 @@
           and string_list(profile.get('remove')) and isinstance(tools, dict) and tools
           and string_list(profile.get('default_tools', [])) and set(profile.get('default_tools', [])) <= set(tools)
           and isinstance(profile.get('choices', {}), dict) and set(profile.get('choices', {})) <= set(choices)
-          and profile.get('manifest') in ('pyproject.toml', 'package.json')
+          and profile.get('manifest') in ('pyproject.toml', 'package.json', 'Cargo.toml')
           and isinstance(init, dict)
           and (not init or (plain_path(init.get('file')) and string_list(init.get('command')) and init['command']))
           and (profile.get('uv_default_groups') is None or isinstance(profile['uv_default_groups'], str)
@@ -180,7 +180,7 @@
           and all(plain_path(k) and plain_path(v) for k, v in base.get('files', {}).items())
           and lines_ok(base)
           and all(re.fullmatch(r'[a-z][a-z0-9_-]*', tool_name) for tool_name in tools)
-          and all(isinstance(tool, dict) and string_list(tool.get('packages')) and tool['packages']
+          and all(isinstance(tool, dict) and string_list(tool.get('packages'))
                   and isinstance(tool.get('files', {}), dict)
                   and all(plain_path(k) and plain_path(v) for k, v in tool.get('files', {}).items())
                   and lines_ok(tool) and parts_ok(tool)
@@ -901,6 +901,10 @@
             groups = tomllib.loads(path.read_text()).get('dependency-groups', {})
             entries = groups.get(group, []) if isinstance(groups, dict) else []
             return {normal_name(item, profile) for item in entries if isinstance(item, str)}
+        if manifest == 'Cargo.toml':
+            # cargo add --dev puts a crate in [dev-dependencies]. A tool of the rust profile may have no crate.
+            table = tomllib.loads(path.read_text()).get('dev-dependencies', {})
+            return set(table) if isinstance(table, dict) else None
         data = json.loads(path.read_text())
         lists = {kind: set(data[kind]) if isinstance(data.get(kind), dict) else set()
                  for kind in ('dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies')}
@@ -943,6 +947,11 @@
             if tool in profile['tools']:
                 entry = profile['tools'][tool]
                 group = entry.get('group', tool)
+                if not entry['packages']:
+                    # A tool with no packages, as each tool of the rust profile, needs no read of the project file.
+                    steps.append(('add', name, tool, None, entry.get('files', {}), entry,
+                                  (owner_key(name, profile, tool), []), None))
+                    continue
                 present = present_packages(target, profile, group)
                 if present is None:
                     steps.append(('add', name, tool, None, {}, entry, (owner_key(name, profile, tool), []),
@@ -1522,7 +1531,8 @@
                 continue
             for first in entry.get('before', []):
                 print('Would run: ' + ' '.join(first))
-            print('Would run: ' + ' '.join(command) if command else f'The project already has the packages of {tool}')
+            print('Would run: ' + ' '.join(command) if command else f'The project already has the packages of {tool}'
+                  if entry.get('packages') else f'{tool} needs no packages')
         after = {}
         sync_files(target, profiles, [n for n in active if n not in leaving], tools, active, True, parts, roles,
                    gone_roles, unsure, migrating, after=after)
@@ -1645,7 +1655,8 @@
                           file=sys.stderr)
                     failed = subprocess.run(command, cwd=target, env=project_env()).returncode
                 else:
-                    print(f'The project already has the packages of {tool} for {profile}', file=sys.stderr)
+                    print(f'The project already has the packages of {tool} for {profile}' if entry.get('packages')
+                          else f'{tool} for {profile} needs no packages', file=sys.stderr)
         except (OSError, StackError) as exc:
             failed = str(exc)
         except BaseException:
```

The tests of jnterfaces need a profile `rust` in `PROFILES` and in `RECORDS_PROFILE` of `tests/test_install_stack.py`, with a `Cargo.toml` case for `present_packages`.

## What jnterfaces reads from the profile

- **The key `code`** of `profile.json` holds the `@code` globs, here `**/*.rs`. The gate reads them for the required targets. The user approved the name `code`. No code of jnterfaces reads the key yet. The Python and TypeScript profiles need the same key.
- **The key `tests`** of `profile.json` holds the globs of the test files, here `tests/**`, the folder `tests` at the top of the project. A wider glob such as `**/tests/**` would also take a module in `src/` and bring its new code into the parent. `test-parent` copies only those files into the worktree of the parent. No code of jnterfaces reads the key yet.
- **The step names** are the recipe names. The profile gives `fmt-check`, `lint`, `typecheck`, `test`, `coverage-check` and `docstring-check`, and also `test-paths` for the `test-parent` recipe.
- **The key `verify`** of each tool lists the steps of the tool, so `just rust verify` runs all six steps today.
- **The folder `.agents/targets/rust/`** is an extra of `skill-package.json`, so the installer copies it as it copies `.agents/just/rust/`.

## Problems for jnterfaces

- **The module sets `working-directory`.** The installer writes `set working-directory := '../..'` into each module, so that the recipes run at the top of the project. The spec allows that setting in a module, and it forbids only a `[working-directory]` attribute on a recipe. The recipes of this profile need the setting.
- **A module does not take the shell of the root Justfile.** With just 1.50, a `set shell` in the root Justfile reaches the recipes of the root and of its imports, but not the recipes of a `mod`. The recipes of the profile are in the module `rust.just`, so the installer must write `set shell` into that file, as the spec says. The lab checks the recipes with a stand-in `jlog` and a `set shell` in the module.
- **The run ID.** The index runs `just --justfile ... rust test` and `just --justfile ... rust coverage-check` as two calls of `just`, so a dependency of `coverage-check` on `test` would run the tests twice. The spec now gives each run of `just` one run ID. The root Justfile has the line `export JLOG_RUN := env("JLOG_RUN", uuid())`, and no module may have it. `test` saves the run ID and the worktree tree next to its report, and `coverage-check` reads the report only when both match. Otherwise it runs the tests itself. So jnterfaces must write the line into the root Justfile. Without it, `coverage-check` runs the tests each time, and `just rust verify` runs them twice.
