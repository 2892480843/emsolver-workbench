#!/usr/bin/env python3
"""Run ONLY after solver completion, in an evaluator-controlled environment."""
import argparse
import json
from emsolver.evaluation import evaluate_results

p=argparse.ArgumentParser(description='隔离的事后参考评测，不向求解器传入参考')
p.add_argument('--candidate',required=True)
p.add_argument('--reference',required=True)
p.add_argument('--lambda-atol',type=float,default=1e-8)
p.add_argument('--lambda-rtol',type=float,default=1e-6)
p.add_argument('--angle-tol',type=float,default=1e-4)
args=p.parse_args()
result=evaluate_results(args.candidate,args.reference,args.lambda_atol,args.lambda_rtol,args.angle_tol)
print(json.dumps(result,ensure_ascii=False,indent=2))
raise SystemExit(0 if result['passed'] else 7)
