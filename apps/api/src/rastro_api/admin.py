"""Development helper: create a repository row and issue its API token.

    uv run python -m rastro_api.admin issue-token --github-id 1 --full-name owner/repo

Repositories are normally created by the GitHub App installation (phase 4); until then this is
how a token for `rastro init` is obtained. The token is printed once; only its hash is stored.
"""

import argparse
import asyncio

from sqlalchemy.dialects.postgresql import insert

from rastro_db import create_engine, create_session_factory, generate_token, hash_token
from rastro_db.models import Repository


async def issue_token(github_id: int, full_name: str, default_branch: str) -> str:
    token = generate_token()
    engine = create_engine()
    try:
        async with create_session_factory(engine)() as session, session.begin():
            statement = insert(Repository).values(
                github_id=github_id,
                full_name=full_name,
                installation_id=0,
                default_branch=default_branch,
                api_token_hash=hash_token(token),
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["github_id"],
                    set_={"api_token_hash": statement.excluded.api_token_hash},
                )
            )
    finally:
        await engine.dispose()
    return token


def main() -> None:
    parser = argparse.ArgumentParser(prog="rastro_api.admin")
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue-token", help="create/update a repository and print a token")
    issue.add_argument("--github-id", type=int, required=True)
    issue.add_argument("--full-name", required=True)
    issue.add_argument("--default-branch", default="main")
    args = parser.parse_args()
    print(asyncio.run(issue_token(args.github_id, args.full_name, args.default_branch)))


if __name__ == "__main__":
    main()
