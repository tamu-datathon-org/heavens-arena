"""
Rishab Jadhav

Agent Tournament Scheduler
Generate matches and submit as kube jobs to k3s cluster
"""

import psycopg2
import subprocess
import json
import time
import sys
from itertools import combinations
from typing import List, Dict, Tuple

DB_CONFIG = {
    'host': 'localhost',
    'port': 5433,
    'database': 'postgres',
    'user': 'postgres',
    'password': 'mysecretpassword'
}

# TODO: define constants
# prior to changing this, create GCP project, provision vm, and set up artifact registry with judge engine image
GCP_PROJECT_ID = "gen-lang-client-0775243386"
VM_INTERNAL_IP = "10.128.0.30"
JUDGE_ENGINE_IMAGE = f"us-central1-docker.pkg.dev/{GCP_PROJECT_ID}/snake-agents/judge-engine:v2.0"

# open DB and read in all agents into memory
def get_agents() -> List[Dict]:
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    
    cur.execute("SELECT agent_id, team_name, image_name FROM agents ORDER BY agent_id")
    agents = [
        {
            'agent_id': row[0],
            'team_name': row[1],
            'image_name': row[2]
        }
        for row in cur.fetchall()
    ]
    
    cur.close()
    conn.close()
    
    print(f"Loaded {len(agents)} agents from database")
    for agent in agents:
        print(f"    + {agent['agent_id']}: {agent['team_name']} ({agent['image_name']})")
    
    return agents

# GENERATES ROUND ROBIN match pairings, adjust/redefine function for a different tournament style
def generate_match_pairings(agents: List[Dict]) -> List[Tuple[Dict, Dict]]:
    pairings = list(combinations(agents, 2))
    print(f"\n  Generated {len(pairings)} unique match pairings")
    return pairings

# pre-pull all agent images + judge engine image
def pull_all_images(agents: List[Dict]) -> bool:
    print("Pulling all images from artifact registry")
    
    print(f"\n[0/{len(agents) + 1}] Pulling judge engine...")
    print(f"  Image: {JUDGE_ENGINE_IMAGE}")
    try:
        result = subprocess.run(
            ['docker', 'pull', JUDGE_ENGINE_IMAGE],
            capture_output=True,
            check=True
        )
        print("Judge engine pulled successfully")
    except subprocess.CalledProcessError as e:
        print(f"Failed to pull judge engine: {e.stderr.decode()}")
        return False
    
    # pull all agent images
    failed_pulls = []
    for i, agent in enumerate(agents, 1):
        print(f"\n[{i}/{len(agents)}] Pulling {agent['team_name']}...")
        print(f"  Image: {agent['image_name']}")
        
        try:
            result = subprocess.run(
                ['docker', 'pull', agent['image_name']],
                capture_output=True,
                check=True,
                timeout=60
            )
            print(f"Pulled successfully")
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr.decode()
            print(f"Pull failed: {error_msg}")
            failed_pulls.append((agent['team_name'], error_msg))
        except subprocess.TimeoutExpired:
            print(f"Pull timed out after 1 minute")
            failed_pulls.append((agent['team_name'], "Timeout"))
    
    if failed_pulls:
        print("\nThese images failed to pull:")
        for team, error in failed_pulls:
            print(f"  - {team}: {error}")
        
        response = input("\nContinue anyway? Matches with missing images will fail. (yes/no): ")
        if response.lower() != 'yes':
            return False
    
    print("\nAll images pulled successfully")
    return True

# generate kubernetes job yaml for a match
def create_job_manifest(match_number: int, agent_a: Dict, agent_b: Dict) -> str:
    
    # standardized job names
    job_name = f"match-{match_number}-{agent_a['team_name']}-vs-{agent_b['team_name']}"
    job_name = job_name.lower().replace('_', '-').replace(' ', '-')

    if len(job_name) > 63:
        job_name = f"match-{match_number}-{agent_a['agent_id']}-vs-{agent_b['agent_id']}"
    

''' Some important details about the yaml blueprint:
    * backoffLimit=0, no retries for failed jobs
    * ttlSecondsAfterFinished=300, successful jobs are cleaned up after 5 mins
    * imagePullSecrets, ensure secret is defined with container registry so we can pull private images
    * securityContext, containers run as non-root, security consideration
    * shared volume is shared memory for pods, starts empty
    * restartPolicy=Never, don't retry failed jobs
    * init agents A and B to ports 8081 and 8082, spin up python web server agents, judge connects to url
    * security hardening on agents, drop linux capabilities, prevent privilege escalation, proper sandboxing aint it
'''
    manifest = f"""apiVersion: batch/v1
kind: Job
metadata:
  name: {job_name}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 300
  template:
    spec:
      imagePullSecrets:
        - name: gcr-secret
      securityContext:
        runAsUser: 1000
        runAsGroup: 1000
        seccompProfile:
          type: RuntimeDefault
      volumes:
        - name: tmp-volume
          emptyDir: {{}}
      restartPolicy: Never
      containers:
        # --- JUDGE ENGINE CONTAINER ---
        - name: judge-engine
          image: {JUDGE_ENGINE_IMAGE}
          command: ["/bin/sh", "-c"]
          args:
            - |
              python judge_engine.py
              EXIT_CODE=$?
              echo "Judge engine completed with exit code $EXIT_CODE"
              echo "Signaling agents to shut down..."
              touch /tmp/game-complete
              sleep 2
              exit $EXIT_CODE
          volumeMounts:
            - name: tmp-volume
              mountPath: /tmp
          env:
            - name: PLAYER1_URL
              value: "http://localhost:8081"
            - name: PLAYER2_URL
              value: "http://localhost:8082"
            - name: DB_USER
              value: "postgres"
            - name: DB_PASSWORD
              value: "mysecretpassword"
            - name: DB_NAME
              value: "postgres"
            - name: DB_HOST
              value: "{VM_INTERNAL_IP}"
            - name: DB_PORT
              value: "5433"
            - name: AGENT_A_ID
              value: "{agent_a['agent_id']}"
            - name: AGENT_B_ID
              value: "{agent_b['agent_id']}"
          securityContext:
            capabilities:
              drop: ["ALL"]
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
        
        # --- AGENT A CONTAINER ---
        - name: agent-a
          image: {agent_a['image_name']}
          command: ["/bin/sh", "-c"]
          args:
            - |
              # Start the Flask agent in the background
              python agent.py &
              AGENT_PID=$!
              
              # Wait for game completion signal
              while [ ! -f /tmp/game-complete ]; do
                # Check if agent process died
                if ! kill -0 $AGENT_PID 2>/dev/null; then
                  echo "Agent process died unexpectedly"
                  exit 1
                fi
                sleep 1
              done
              
              echo "Game complete signal received, shutting down agent-a"
              kill $AGENT_PID 2>/dev/null || true
              wait $AGENT_PID 2>/dev/null || true
              exit 0
          volumeMounts:
            - name: tmp-volume
              mountPath: /tmp
          env:
            - name: PORT
              value: "8081"
          securityContext:
            capabilities:
              drop: ["ALL"]
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
        
        # --- AGENT B CONTAINER ---
        - name: agent-b
          image: {agent_b['image_name']}
          command: ["/bin/sh", "-c"]
          args:
            - |
              # Start the Flask agent in the background
              python agent.py &
              AGENT_PID=$!
              
              # Wait for game completion signal
              while [ ! -f /tmp/game-complete ]; do
                # Check if agent process died
                if ! kill -0 $AGENT_PID 2>/dev/null; then
                  echo "Agent process died unexpectedly"
                  exit 1
                fi
                sleep 1
              done
              
              echo "Game complete signal received, shutting down agent-b"
              kill $AGENT_PID 2>/dev/null || true
              wait $AGENT_PID 2>/dev/null || true
              exit 0
          volumeMounts:
            - name: tmp-volume
              mountPath: /tmp
          env:
            - name: PORT
              value: "8082"
          securityContext:
            capabilities:
              drop: ["ALL"]
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
"""
    
    return manifest

# submit job to kube cluster
def submit_job(manifest: str) -> bool:
    try:
        result = subprocess.run(
            ['kubectl', 'apply', '-f', '-'],
            input=manifest.encode(),
            capture_output=True,
            check=True
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"    Failed to submit job: {e.stderr.decode()}")
        return False

def get_job_status() -> Dict:
    try:
        result = subprocess.run(
            ['kubectl', 'get', 'jobs', '-o', 'json'],
            capture_output=True,
            check=True
        )
        jobs = json.loads(result.stdout)
        
        active = 0
        succeeded = 0
        failed = 0
        
        for job in jobs.get('items', []):
            status = job.get('status', {})
            active += status.get('active', 0)
            succeeded += status.get('succeeded', 0)
            failed += status.get('failed', 0)
        
        return {
            'active': active,
            'succeeded': succeeded,
            'failed': failed,
            'total': len(jobs.get('items', []))
        }
    except Exception as e:
        print(f"Warning: Could not check job status: {e}")
        return {'active': 0, 'succeeded': 0, 'failed': 0, 'total': 0}

def run_tournament():
    print("Welcome to Heavens Arena!")
    
    # Load agents from database
    agents = get_agents()
    if len(agents) < 2:
        print("\nERROR: Need at least 2 agents to run tournament")
        sys.exit(1)
    
    # Generate all match pairings
    pairings = generate_match_pairings(agents)
    
    print(f"\n Tournament Summary:")
    print(f"   Number of Teams: {len(agents)}")
    print(f"   Total Matches: {len(pairings)}")
    
    response = input("\nProceed with image pre-pull and tournament? (yes/no): ")
    if response.lower() != 'yes':
        print("Tournament cancelled")
        sys.exit(0)
    
    if not pull_all_images(agents):
        print("\nImage pre-pull failed or cancelled")
        sys.exit(1)
    
    print("Submitting matches to cluster:")
    submitted = 0
    failed = 0
    
    for match_num, (agent_a, agent_b) in enumerate(pairings, 1):
        print(f"\n[{match_num}/{len(pairings)}] {agent_a['team_name']} vs {agent_b['team_name']}")
        print(f"  Agent IDs: {agent_a['agent_id']} vs {agent_b['agent_id']}")
        
        # Create and submit job
        manifest = create_job_manifest(match_num, agent_a, agent_b)
        
        if submit_job(manifest):
            submitted += 1
            print(f"  Job submitted")
        else:
            failed += 1
            print(f"  Job submission failed")
        
        time.sleep(0.5)
    
    print("Tournament Progress Logging:")
    
    last_status = None
    while True:
        status = get_job_status()
        
        if status != last_status:
            completed = status['succeeded'] + status['failed']
            print(f"\nStatus: {completed}/{len(pairings)} completed | "
                  f"{status['active']} running | "
                  f"{status['succeeded']} succeeded | "
                  f"{status['failed']} failed")
            last_status = status
        
        if status['succeeded'] + status['failed'] >= submitted:
            break
        
        time.sleep(10)
    
    final_status = get_job_status()
    print("Tournament complete!")
    print(f"   Submitted: {submitted}")
    print(f"   Succeeded: {final_status['succeeded']}")
    print(f"   Failed: {final_status['failed']}")
    
    if final_status['failed'] > 0:
        print("\n⚠️  Some matches failed. Check logs with:")
        print("   kubectl get jobs --field-selector status.successful=0")
        print("   kubectl logs job/<job-name> -c judge-engine")

if __name__ == "__main__":
    try:
        run_tournament()
    except KeyboardInterrupt:
        print("\n\n Tournament interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
