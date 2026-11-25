"""One head-bound GraphQL observation, with no hidden pagination or retries. [daveey]"""
import re

QUERY = """query($owner:String!,$name:String!,$number:Int!) {
  repository(owner:$owner,name:$name) { pullRequest(number:$number) {
    state headRefOid reviewDecision author { login }
    reviews(last:100) { pageInfo { hasPreviousPage }
      nodes { state body author { login __typename } commit { oid } } }
    commits(last:1) { nodes { commit { oid statusCheckRollup {
      contexts(first:100) { pageInfo { hasNextPage } nodes { __typename
        ... on CheckRun { isRequired(pullRequestNumber:$number) status conclusion }
        ... on StatusContext { isRequired(pullRequestNumber:$number) state }
      } }
    } } } }
  } }
}"""


def argv(url):
    match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)", url)
    if not match:
        return None
    owner, name, number = match.groups()
    return ["api", "graphql", "-f", "query=" + QUERY, "-f", "owner=" + owner,
            "-f", "name=" + name, "-F", "number=" + number]


def bucket(node):
    """Match gh pr checks buckets; unknown provider enums never establish green."""
    if not isinstance(node, dict) or type(node.get("isRequired")) is not bool:
        return None
    if node.get("__typename") == "CheckRun":
        status = node.get("status")
        state = node.get("conclusion") if status == "COMPLETED" else status
    elif node.get("__typename") == "StatusContext":
        state = node.get("state")
    else:
        return None
    if state in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
        return "pass"
    if state in {"ERROR", "FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}:
        return "fail"
    if state in {"PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "PENDING_DEPLOYMENT"}:
        return "pending"
    # Cancelled is not green (gh's separate cancel bucket is likewise not pass).
    return "cancel" if state == "CANCELLED" else None


def parse(payload):
    """Reject partial errors, pagination and mismatched commit/check identities."""
    if not isinstance(payload, dict) or payload.get("errors"):
        return None
    try:
        data = dict(payload["data"]["repository"]["pullRequest"])
        if data.get("state") in {"MERGED", "CLOSED"}:
            return data, []
        if data.get("state") != "OPEN" or not data.get("headRefOid"):
            return None
        # gh pr view represents GraphQL's null reviewDecision as an empty string.
        if data.get("reviewDecision") is None:
            data["reviewDecision"] = ""
        reviews = data["reviews"]
        if reviews["pageInfo"]["hasPreviousPage"] is not False or not isinstance(reviews["nodes"], list):
            return None
        if not all(isinstance(review, dict) for review in reviews["nodes"]):
            return None
        data["reviews"] = reviews["nodes"]
        for review in data["reviews"]:
            author = review.get("author")
            if isinstance(author, dict) and author.get("__typename") == "Bot":
                review["author"] = dict(author, is_bot=True)
        commits = data["commits"]["nodes"]
        if len(commits) != 1 or commits[0]["commit"]["oid"] != data["headRefOid"]:
            return None
        contexts = commits[0]["commit"]["statusCheckRollup"]["contexts"]
        if contexts["pageInfo"]["hasNextPage"] is not False or not isinstance(contexts["nodes"], list):
            return None
        buckets = []
        for node in contexts["nodes"]:
            value = bucket(node)
            if value is None:
                return None
            if node["isRequired"]:
                buckets.append({"bucket": value})
        return data, buckets
    except (KeyError, TypeError, ValueError):
        return None
