"""Human workflow commands; no AI dispatch or arbitrary command endpoint."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from . import user_acceptance, workflow_setup
from .config import load_project


def configure(sub, root):
    def run(args):
        project_root = root(args.path)
        if args.action == "show":
            project = load_project(project_root)
            print(json.dumps({"workflow": project.raw.get("workflow"),
                              "model_policy": project.raw.get("model_policy")}, indent=2))
        elif args.action == "apply":
            profile = yaml.safe_load(Path(args.file).read_text(encoding="utf-8"))
            print(json.dumps(workflow_setup.apply(project_root, profile, replan_unstarted=args.replan_unstarted)))
        elif args.action == "import-job":
            request = yaml.safe_load(Path(args.file).read_text(encoding="utf-8"))
            print(json.dumps(workflow_setup.import_job(project_root, request)))
        elif args.action == "preview":
            from . import db
            conn = db.connect_readonly(project_root)
            try:
                print(json.dumps(user_acceptance.preview(conn, load_project(project_root), args.job), indent=2))
            finally:
                conn.close()
        else:
            evidence = Path(args.evidence_file).read_text(encoding="utf-8")
            result = user_acceptance.decide(project_root, args.job, args.revision, args.head,
                                            args.digest, args.verdict, evidence)
            print(json.dumps(result))
        return 0
    parser = sub.add_parser("workflow", help="select review policy, submit a job, and review exact previews")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("show").set_defaults(func=run)
    for name in ("apply", "import-job"):
        command = commands.add_parser(name)
        command.add_argument("--file", required=True)
        if name == "apply":
            command.add_argument("--replan-unstarted", metavar="JOB", help="explicitly convert one never-started planning job; forces execution pause and requires replanning")
        command.set_defaults(func=run)
    command = commands.add_parser("preview")
    command.add_argument("job")
    command.set_defaults(func=run)
    command = commands.add_parser("decide")
    command.add_argument("job")
    command.add_argument("--revision", type=int, required=True)
    command.add_argument("--head", required=True)
    command.add_argument("--digest", required=True)
    command.add_argument("--verdict", choices=("PASS", "CHANGES"), required=True)
    command.add_argument("--evidence-file", required=True)
    command.set_defaults(func=run)
