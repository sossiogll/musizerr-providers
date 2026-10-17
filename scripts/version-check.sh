#!/bin/sh
# Check that every provider a change touches has a greater version than before it: the CI job "provider version", on
# every pull request into dev.
#
#   scripts/version-check.sh <base> [<head>]
#
#   <base>  the commit the change is compared with (in CI the first parent of the pull request's merge commit: dev)
#   <head>  the changed commit (default HEAD)
#
# A provider folder (one with a pyproject.toml and a Dockerfile) in which a file changed, other than a Markdown file or
# a file under a docs/ folder, must have a greater `version` in its pyproject.toml than on <base>: that version is the
# tag of its image, and the image of a version that is not published yet is published when the change reaches dev
# (.github/workflows/publish.yml). Everything in the folder counts, the packages a provider is made of included. A new
# provider has nothing to compare with and passes; a removed one is not checked. Changes outside the provider folders
# (scripts, .github, README.md, catalog.json) need no new version. Versions are compared as `sort -V` does (1.10.0 is
# greater than 1.9.0). The exit status is non-zero if a provider needs a new version.
set -eu

[ $# -ge 1 ] || { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
BASE=$1
HEAD=${2:-HEAD}
git rev-parse -q --verify "$BASE^{commit}" >/dev/null || { echo "$BASE is not a commit" >&2; exit 2; }
git rev-parse -q --verify "$HEAD^{commit}" >/dev/null || { echo "$HEAD is not a commit" >&2; exit 2; }

error() {
  if [ "${GITHUB_ACTIONS:-}" = true ]; then echo "::error file=$1/pyproject.toml::$2"; else echo "$1: $2" >&2; fi
}
is_provider() { git cat-file -e "$1:$2/pyproject.toml" 2>/dev/null && git cat-file -e "$1:$2/Dockerfile" 2>/dev/null; }
version_at() { git show "$1:$2/pyproject.toml" | sed -n 's/^version = "\(.*\)"$/\1/p' | head -n 1; }
# True when $1 is a greater version than $2.
greater() { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n 1)" = "$1" ]; }

# The top-level folders with a change that counts.
folders=$(git diff --name-only "$BASE" "$HEAD" | while IFS= read -r path; do
  case $path in
    */*) ;;
    *) continue ;;
  esac
  case $path in
    *.md|docs/*|*/docs/*) continue ;;
  esac
  printf '%s\n' "${path%%/*}"
done | sort -u)

failed=0
checked=0
for folder in $folders; do
  is_provider "$HEAD" "$folder" || continue
  checked=$((checked + 1))
  new=$(version_at "$HEAD" "$folder")
  if [ -z "$new" ]; then
    error "$folder" "cannot read the version of $folder/pyproject.toml"
    failed=1
    continue
  fi
  if ! is_provider "$BASE" "$folder"; then
    echo "$folder: new provider, version $new"
    continue
  fi
  old=$(version_at "$BASE" "$folder")
  if [ -n "$old" ] && ! greater "$new" "$old"; then
    error "$folder" "$folder changed but its version is still $new (it is $old on the base): raise \"version\" in $folder/pyproject.toml, its image is published under that version"
    failed=1
  else
    echo "$folder: ${old:-?} -> $new"
  fi
done
[ "$checked" -gt 0 ] || echo "no provider changed: no version to raise"
exit $failed
