USER_NAME="ericcccc2021"
echo "User" "$USER_NAME"
git log --author="$USER_NAME" --pretty=tformat: --numstat | awk '{ add += $1; subs += $2; loc += $1 - $2 } END { printf "added lines: %s, removed lines: %s, total lines: %s\n", add, subs, loc }' -
echo "commit counts:"
git shortlog -s