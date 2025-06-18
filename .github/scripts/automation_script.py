import os
import requests
import time
import random
import datetime
import base64 # Standard Python library, no pip install needed

# --- Configuration from Environment Variables ---
# These are passed from the GitHub Actions workflow environment
GITHUB_TOKEN = os.getenv('GH_TOKEN_SCRIPT')
REPO_OWNER = os.getenv('REPO_OWNER')
REPO_NAME = os.getenv('REPO_NAME')
LOG_FILE_PATH = os.getenv('LOG_FILE_PATH')

# --- GitHub API Constants ---
BASE_API_URL = "https://api.github.com"
HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "X-GitHub-Api-Version": "2022-11-28" # Specify API version for stability
}

# --- Helper Functions for API Interaction ---

def github_api_call(method, url, **kwargs):
    """Handles GitHub API calls with basic error checking and retries."""
    full_url = f"{BASE_API_URL}{url}"
    print(f"Making {method} request to: {full_url}")
    max_retries = 5
    for attempt in range(max_retries):
        try:
            response = requests.request(method, full_url, headers=HEADERS, **kwargs)
            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
            return response
        except requests.exceptions.HTTPError as e:
            print(f"  Attempt {attempt + 1}/{max_retries}: GitHub API Error: {e.response.status_code} - {e.response.text}")
            if e.response.status_code == 403 and 'rate limit exceeded' in e.response.text.lower():
                # Handle rate limit specifically
                reset_time = int(e.response.headers.get('X-RateLimit-Reset', time.time() + 60))
                sleep_duration = max(reset_time - int(time.time()) + 1, 10) # Sleep until reset + a buffer
                print(f"  Rate limit hit. Waiting for {sleep_duration} seconds until reset.")
                time.sleep(sleep_duration)
            elif e.response.status_code in [404, 409] or attempt == max_retries - 1:
                # For 404 (Not Found) or 409 (Conflict) or last attempt, re-raise immediately
                raise
            else:
                # For other transient errors, retry with exponential backoff
                sleep_duration = 2 ** attempt
                print(f"  Retrying in {sleep_duration} seconds...")
                time.sleep(sleep_duration)
        except requests.exceptions.ConnectionError as e:
            print(f"  Attempt {attempt + 1}/{max_retries}: Connection Error: {e}")
            if attempt == max_retries - 1:
                raise
            sleep_duration = 2 ** attempt
            print(f"  Retrying in {sleep_duration} seconds...")
            time.sleep(sleep_duration)
    raise Exception("Failed to make API call after multiple retries.") # Should not be reached

def get_repo_url():
    return f"/repos/{REPO_OWNER}/{REPO_NAME}"

def get_ref_url(ref_type, ref_name):
    # This is for GETting specific refs like 'heads/main' or 'tags/v1.0'
    return f"/repos/{REPO_OWNER}/{REPO_NAME}/git/refs/{ref_type}/{ref_name}"

def get_contents_url(path):
    return f"/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"

def get_pulls_url():
    return f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls"

def get_pull_merge_url(pr_number):
    return f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{pr_number}/merge"

# --- Core Automation Functions ---

def create_repository_if_not_exists():
    """Checks if the repo exists, creates it if not. Sets 'private' accordingly."""
    print(f"Checking for repository: {REPO_OWNER}/{REPO_NAME}")
    try:
        github_api_call("GET", get_repo_url())
        print(f"Repository '{REPO_NAME}' already exists.")
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            print(f"Repository '{REPO_NAME}' not found. Creating it...")
            data = {
                "name": REPO_NAME,
                "description": "Automated repository for Pull Shark achievement (managed by GitHub Action).",
                "private": True, # Set to True for a private repo as requested
                "auto_init": True # Initializes with a README.md, which is useful
            }
            # For user-owned repos, use /user/repos. For organization-owned, use /orgs/{org}/repos
            github_api_call("POST", "/user/repos", json=data)
            print(f"Repository '{REPO_NAME}' created successfully as private.")
            # Give GitHub a moment to fully initialize the new repo and its default branch
            print("Waiting for repository initialization...")
            time.sleep(15) # Increased sleep for fresh repo creation
            # Verify main branch exists after creation
            retries = 10
            for i in range(retries):
                try:
                    get_main_branch_sha() # This will raise if not found
                    print("Main branch confirmed ready.")
                    break
                except requests.exceptions.HTTPError as branch_e:
                    if branch_e.response.status_code == 404 and i < retries - 1:
                        print(f"Main branch not ready yet ({i+1}/{retries}). Retrying in {2**(i)} seconds...")
                        time.sleep(2**i) # Exponential backoff for branch availability
                    else:
                        raise # Re-raise if it's a persistent error or last retry
        else:
            raise # Re-raise any other unexpected HTTP errors

def get_main_branch_sha():
    """Gets the SHA of the default branch (main or master)."""
    print("Getting SHA of the default branch (main/master)...")
    try:
        response = github_api_call("GET", get_ref_url("heads", "main"))
        print("Using 'main' branch.")
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            # Try 'master' if 'main' doesn't exist
            print("Main branch not found, trying 'master' branch.")
            response = github_api_call("GET", get_ref_url("heads", "master"))
            print("Using 'master' branch.")
        else:
            raise
    return response.json()['object']['sha']

def create_branch(branch_name, base_sha):
    """Creates a new branch from a base SHA."""
    print(f"Creating branch: {branch_name}")
    data = {
        "ref": f"refs/heads/{branch_name}",
        "sha": base_sha
    }
    # THIS IS THE CRUCIAL FIX: Correct API endpoint for creating a new Git Ref (branch)
    github_api_call("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/git/refs", json=data)
    print(f"Branch '{branch_name}' created.")

def get_file_content(file_path, branch="main"):
    """Gets the content and SHA of a file from a specific branch, or None if not found."""
    try:
        # We need to specify the branch when getting content if it's not the default
        response = github_api_call("GET", f"{get_contents_url(file_path)}?ref={branch}")
        content = base64.b64decode(response.json()['content']).decode('utf-8')
        sha = response.json()['sha']
        return content, sha
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return None, None # File not found
        else:
            raise

def update_file_and_commit(branch_name, file_path, content, commit_message, current_sha=None):
    """Creates or updates a file and commits it to the specified branch."""
    print(f"Updating/Creating file '{file_path}' and committing on branch '{branch_name}'")
    data = {
        "message": commit_message,
        "content": base64.b64encode(content.encode('utf-8')).decode('utf-8'),
        "branch": branch_name,
    }
    if current_sha:
        data["sha"] = current_sha # Required when updating an existing file

    response = github_api_call("PUT", get_contents_url(file_path), json=data)
    print(f"File '{file_path}' committed.")
    return response.json()['commit']['sha'] # Return the commit SHA

def create_pull_request(head_branch, base_branch, title, body):
    """Creates a pull request."""
    print(f"Creating pull request from '{head_branch}' to '{base_branch}'")
    data = {
        "title": title,
        "body": body,
        "head": head_branch,
        "base": base_branch
    }
    response = github_api_call("POST", get_pulls_url(), json=data)
    pr_number = response.json()['number']
    print(f"Pull request #{pr_number} created.")
    return pr_number

def merge_pull_request(pr_number):
    """Merges a pull request."""
    print(f"Merging pull request #{pr_number}")
    data = {
        "commit_title": f"Merge pull request #{pr_number} (Automated)",
        "commit_message": "Automated merge for Pull Shark achievement.",
        "merge_method": "merge" # Can be 'merge', 'squash', or 'rebase'
    }
    response = github_api_call("PUT", get_pull_merge_url(pr_number), json=data)
    print(f"Pull request #{pr_number} merged successfully.")

def delete_branch(branch_name):
    """Deletes a branch."""
    print(f"Deleting branch: {branch_name}")
    try:
        github_api_call("DELETE", get_ref_url("heads", branch_name))
        print(f"Branch '{branch_name}' deleted.")
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 422: # Unprocessable Entity, often for protected branches or in-use branches
            print(f"Could not delete branch '{branch_name}'. It might be protected or in use. Status code: {e.response.status_code}")
        else:
            raise

def log_event(message):
    """Appends a message to the automation log file in the repository."""
    # Get current time in IST (UTC+5:30)
    current_time_utc = datetime.datetime.now(datetime.timezone.utc)
    ist_offset = datetime.timedelta(hours=5, minutes=30)
    current_time_ist = (current_time_utc + ist_offset).strftime("%Y-%m-%d %H:%M:%S IST")

    log_entry = f"- {current_time_ist}: {message}\n"

    print(f"Logging event: {log_entry.strip()}")

    # Get current log file content and SHA from the 'main' branch
    # This function is now more robust with branch specification
    current_log_content, current_log_sha = get_file_content(LOG_FILE_PATH, branch="main")

    new_log_content = ""
    if current_log_content:
        new_log_content = current_log_content + log_entry
    else:
        new_log_content = f"# GitHub Automation Log for {REPO_NAME}\n\n" + log_entry

    # Commit the updated log file to the 'main' branch
    update_file_and_commit(
        "main", # Assuming 'main' is the base branch for the log file
        LOG_FILE_PATH,
        new_log_content,
        f"Update automation log: {message.splitlines()[0]}", # Use first line of message as commit summary
        current_log_sha
    )
    print(f"Log file '{LOG_FILE_PATH}' updated.")


# --- Main Execution Logic ---
if __name__ == "__main__":
    # Validate environment variables are set
    if not GITHUB_TOKEN:
        raise ValueError("GH_TOKEN_SCRIPT environment variable is not set. Please ensure your GitHub PAT secret is correctly configured and passed.")
    if not REPO_OWNER or not REPO_NAME or not LOG_FILE_PATH:
        raise ValueError("REPO_OWNER, REPO_NAME, or LOG_FILE_PATH environment variables are not set. Check workflow configuration.")

    try:
        # 1. Ensure the repository exists (create if not). It will be created as private.
        create_repository_if_not_exists()
        main_branch_sha = get_main_branch_sha() # Get SHA after ensuring repo is ready

        timestamp = int(time.time())
        # Add a random component to ensure unique branch names even if runs are very close in time
        pr_identifier = f"pr-{timestamp}-{random.randint(1000, 9999)}"
        branch_name = f"feature/{pr_identifier}"
        file_path_in_pr = f"pr_data/{pr_identifier}.txt" # Path for the file to be changed within the PR
        pr_content = f"Automated data for PR {pr_identifier} created on {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z%z')}."
        commit_message = f"Add data for automated PR {pr_identifier}"
        pr_title = f"Automated Pull Request: {pr_identifier}"
        pr_body = f"This pull request ({pr_identifier}) is part of an automated process to track pull request merges for the Pull Shark achievement."

        # 2. Create a new branch for the PR
        create_branch(branch_name, main_branch_sha)

        # 3. Create/Update a file on that new branch
        # Get the current SHA of the newly created branch to use as the parent for the commit
        current_branch_ref_response = github_api_call("GET", get_ref_url("heads", branch_name))
        current_branch_sha_for_commit = current_branch_ref_response.json()['object']['sha']

        update_file_and_commit(
            branch_name,
            file_path_in_pr,
            pr_content,
            commit_message,
            current_branch_sha_for_commit # Use the branch's current SHA as the parent
        )

        # 4. Create a Pull Request
        # Assuming 'main' is your base branch for merging into
        pr_number = create_pull_request(branch_name, "main", pr_title, pr_body)

        # Give GitHub a moment to process the PR creation before attempting merge
        time.sleep(5)

        # 5. Merge the Pull Request
        merge_pull_request(pr_number)

        # 6. Log the successful merge to the log file in the repository
        log_event(f"Successfully merged PR #{pr_number} ('{pr_title}') from '{branch_name}'.")

        # 7. Optional: Delete the feature branch after merging to keep the repo clean
        time.sleep(10) # Give GitHub a moment to register the merge before deleting
        # Uncomment the line below if you want to automatically delete branches:
        delete_branch(branch_name)

        print("Automation run completed successfully.")

    except requests.exceptions.HTTPError as e:
        error_message = f"Fatal GitHub API Error: {e.response.status_code} - {e.response.text.splitlines()[0]}"
        print(error_message)
        # Attempt to log failure if possible
        try:
            log_event(f"Failed to complete automation: {error_message}")
        except Exception as log_e:
            print(f"Error during logging of a failure: {log_e}")
        exit(1) # Exit with a non-zero code to indicate failure to GitHub Actions
    except Exception as e:
        error_message = f"An unexpected error occurred: {str(e).splitlines()[0]}"
        print(error_message)
        # Attempt to log failure if possible
        try:
            log_event(f"Failed to complete automation: {error_message}")
        except Exception as log_e:
            print(f"Error during logging of an unexpected failure: {log_e}")
        exit(1) # Exit with a non-zero code to indicate failure
