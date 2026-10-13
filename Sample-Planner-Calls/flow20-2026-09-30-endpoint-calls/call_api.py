"""Replays the six calls of flow20-2026-09-30 against the planner API, in order.
Needs AWS credentials allowed to invoke the API (IAM / SigV4, service execute-api, us-east-1).
Usage: python call_api.py [--test-id my-replay-01]   (pip install boto3 requests)"""
import argparse, json, boto3, requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

URL = "https://wbenhm5d8a.execute-api.us-east-1.amazonaws.com/prod/agent"
ap = argparse.ArgumentParser()
ap.add_argument("--test-id", default="flow20-replay")
ap.add_argument("--profile", default=None)
a = ap.parse_args()
creds = boto3.Session(profile_name=a.profile).get_credentials()
for k in range(1, 7):
    body = json.load(open(f"request_b{k}.json"))
    body["mission"]["test_id"] = a.test_id
    req = AWSRequest(method="POST", url=URL, data=json.dumps(body), headers={"Content-Type": "application/json"})
    SigV4Auth(creds, "execute-api", "us-east-1").add_auth(req)
    r = requests.post(URL, data=req.body, headers=dict(req.headers), timeout=300).json()
    res = r.get("response", r)
    print(f"batch {k}: {res.get('status')} solver={res.get('solver_time_s')}s mission_id={res.get('mission_id')}")
