"""Split a Git preview diff into exact per-path chunks for the JSON API."""

def diff_path_relative(path):
    if not path or path == "/dev/null":
        return None
    path = path[2:] if path.startswith(("a/", "b/")) else path
    for marker in ("/baseline/", "/preview/", "/save-to-git-preview/", "/apply-preview/"):
        if marker in path:
            return path.rsplit(marker, 1)[1]
    return path


def match_preview_path(raw_path, paths, current_target=None):
    relative = diff_path_relative(raw_path)
    if not relative:
        return None
    candidates = [relative]
    if current_target and not relative.startswith(f"{current_target}/"):
        candidates.append(f"{current_target}/{relative}")
    for candidate in candidates:
        for path in paths:
            if candidate == path or candidate.endswith(f"/{path}"):
                return path
    return None


def diff_git_path(line):
    parts = line.split()
    if len(parts) >= 4:
        return parts[3]
    return None


def diff_command_path(line):
    parts = line.split()
    if len(parts) >= 4:
        return parts[-1]
    return None


def diff_header_path(line):
    return line[4:].split("\t", 1)[0]


def split_preview_diff_by_path(detail, paths):
    path_set = set(paths)
    chunks = {}
    summary = []
    current_target = None
    current_path = None
    current_lines = []
    pending_old_path = None

    def flush():
        nonlocal current_path, current_lines
        if not current_lines:
            return
        if current_path in path_set:
            chunks.setdefault(current_path, []).extend(current_lines)
        else:
            summary.extend(current_lines)
        current_lines = []
        current_path = None

    # Diff records use LF; other separators can be literal hunk content.
    lines = detail.replace("\r\n", "\n").split("\n")
    if lines[-1] == "":
        lines.pop()
    for line in lines:
        # target_diff() emits this standalone status between target diffs.
        if line.startswith("Target ") and line.endswith(": no file changes."):
            flush()
            summary.append(line)
            current_target = None
            current_path = None
            pending_old_path = None
            continue
        if line.startswith("## "):
            flush()
            current_target = line[3:].strip()
            summary.append(line)
            continue
        if line.startswith("diff --git "):
            flush()
            current_path = match_preview_path(diff_git_path(line), paths, current_target)
            current_lines = [line]
            pending_old_path = None
            continue
        if line.startswith("diff "):
            flush()
            current_path = match_preview_path(diff_command_path(line), paths, current_target)
            current_lines = [line]
            pending_old_path = None
            continue
        if line.startswith("--- "):
            if current_lines and current_path is None:
                flush()
            if not current_lines:
                current_lines = []
            pending_old_path = diff_header_path(line)
            current_lines.append(line)
            continue
        if line.startswith("+++ "):
            new_path = diff_header_path(line)
            if current_path is None:
                current_path = match_preview_path(new_path, paths, current_target) or match_preview_path(
                    pending_old_path, paths, current_target
                )
            current_lines.append(line)
            continue
        if current_lines:
            current_lines.append(line)
        else:
            summary.append(line)
    flush()
    return {path: "\n".join(lines) for path, lines in chunks.items()}, "\n".join(summary).strip()
