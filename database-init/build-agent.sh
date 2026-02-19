#!/bin/bash

REPO_URL=$1
TEAM_NAME=$2

# change GCP project ID
PROJECT_ID="gen-lang-client-0775243386"

# change container registry location if needed
ARTIFACT_REGISTRY="us-central1-docker.pkg.dev"

# max size of image
MAX_SIZE_BYTES=5368709120
MAX_SIZE_GB=5

# ensure script properly called
if [ -z "$REPO_URL" ] || [ -z "$TEAM_NAME" ]; then
  echo "Usage: ./build_agent.sh <repository_url> <team_name>"
  exit 1
fi

# standardize repo url
REPO_URL=$(echo "$REPO_URL" | sed 's|/tree/[^/]*$||')
# Ensure .git suffix
if [[ ! "$REPO_URL" =~ \.git$ ]]; then
  REPO_URL="${REPO_URL}.git"
fi

echo "--- Building agent for team: $TEAM_NAME from $REPO_URL ---"

# make temporary dir for git clone
BUILD_DIR=$(mktemp -d)
echo "Using temporary build directory: $BUILD_DIR"

echo "Cloning repository..."
if ! git clone "$REPO_URL" "$BUILD_DIR"; then
  echo "ERROR: Failed to clone repository"
  echo "Make sure the URL is correct and the repository is public"
  rm -rf "$BUILD_DIR"
  exit 1
fi

# start agent build
echo "Checking repository structure..."

# look for requirements.txt to determine build directory
if [ -f "$BUILD_DIR/requirements.txt" ]; then
  AGENT_DIR="$BUILD_DIR"
  echo "Found requirements.txt in root directory"
elif [ -f "$BUILD_DIR/agent/requirements.txt" ]; then
  AGENT_DIR="$BUILD_DIR/agent"
  echo "Found requirements.txt in agent/ subdirectory"
elif [ -f "$BUILD_DIR/src/requirements.txt" ]; then
  AGENT_DIR="$BUILD_DIR/src"
  echo "Found requirements.txt in src/ subdirectory"
else
  echo "ERROR: Could not find requirements.txt in repository"
  echo "Searched in:"
  echo "  - $BUILD_DIR/requirements.txt"
  echo "  - $BUILD_DIR/agent/requirements.txt"
  echo "  - $BUILD_DIR/src/requirements.txt"
  echo ""
  echo "Repository contents:"
  ls -la "$BUILD_DIR"
  rm -rf "$BUILD_DIR"
  exit 1
fi

echo "Using build directory: $AGENT_DIR"

# pre-validation for large dependencies, no cuda allows that shit is too fat
echo "Checking for potentially large dependencies..."
if [ -f "$BUILD_DIR/requirements.txt" ]; then
  LARGE_LIBS=("torch" "tensorflow" "cuda" "cupy" "jax")
  for lib in "${LARGE_LIBS[@]}"; do
    if grep -qi "^${lib}" "$BUILD_DIR/requirements.txt"; then
      echo "⚠️  WARNING: Detected '${lib}' in requirements.txt - this may result in a large image"
      read -p "Continue anyway? (y/n) " -n 1 -r
      echo
      if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Build cancelled by user"
        rm -rf "$BUILD_DIR"
        exit 1
      fi
      break
    fi
  done
fi

# final image path
IMAGE_PATH="${ARTIFACT_REGISTRY}/${PROJECT_ID}/snake-agents/${TEAM_NAME}:latest"
LOCAL_TAG="${TEAM_NAME}:build-check"

echo "Building Docker image locally: $LOCAL_TAG"

# local build before pushing
if ! docker buildx build \
  --platform linux/amd64 \
  --load \
  --cache-from=type=local,src=/var/lib/docker-build-cache \
  --cache-to=type=local,dest=/var/lib/docker-build-cache,mode=max \
  -f ./Dockerfile.agent \
  -t "$LOCAL_TAG" \
  "$AGENT_DIR"; then
  echo "ERROR: Docker build failed"
  rm -rf "$BUILD_DIR"
  exit 1
fi

echo "Build complete. Checking image size..."

IMAGE_SIZE=$(docker inspect "$LOCAL_TAG" --format='{{.Size}}')

# ensure image size is below threshold
if [ -z "$IMAGE_SIZE" ]; then
  echo "ERROR: Could not determine image size"
  docker rmi "$LOCAL_TAG" 2>/dev/null
  rm -rf "$BUILD_DIR"
  exit 1
fi

IMAGE_SIZE_GB=$(echo "scale=2; $IMAGE_SIZE / 1073741824" | bc)

echo "Image size: ${IMAGE_SIZE_GB} GB (${IMAGE_SIZE} bytes)"
echo "Size limit: ${MAX_SIZE_GB} GB (${MAX_SIZE_BYTES} bytes)"

if [ "$IMAGE_SIZE" -gt "$MAX_SIZE_BYTES" ]; then
  echo "ERROR: Image size (${IMAGE_SIZE_GB} GB) exceeds the ${MAX_SIZE_GB} GB limit!"
  echo "Please optimize your image by:"
  echo "  - Using a smaller base image (e.g., python:3.10-slim instead of python:3.10)"
  echo "  - Removing unnecessary dependencies"
  echo "  - Using multi-stage builds"
  echo "  - Avoiding CUDA/PyTorch if not absolutely necessary"
  
  # clean up
  docker rmi "$LOCAL_TAG" 2>/dev/null
  rm -rf "$BUILD_DIR"
  exit 1
fi

echo "✓ Image size is within limits"

# tag image for registry
echo "Tagging image for registry: $IMAGE_PATH"
docker tag "$LOCAL_TAG" "$IMAGE_PATH"

# push to artifact registry
echo "Pushing to Artifact Registry..."
if ! docker push "$IMAGE_PATH"; then
  echo "ERROR: Failed to push image to registry"
  docker rmi "$LOCAL_TAG" 2>/dev/null
  docker rmi "$IMAGE_PATH" 2>/dev/null
  rm -rf "$BUILD_DIR"
  exit 1
fi

echo "Successfully pushed: $IMAGE_PATH"

# clean up local images
docker rmi "$LOCAL_TAG" 2>/dev/null

# append sql insert statement to seed.sql
echo "Appending SQL INSERT statement to seed.sql..."

cat <<EOF >> seed.sql
INSERT INTO agents (team_name, image_name) VALUES ('${TEAM_NAME}', '${IMAGE_PATH}');
EOF

if [ $? -ne 0 ]; then
  echo "ERROR: Failed to write to seed.sql"
  rm -rf "$BUILD_DIR"
  exit 1
fi

echo "✓ Added team to seed.sql"

# clean up build directory
rm -rf "$BUILD_DIR"

echo ""
echo "--- Successfully built agent for team: $TEAM_NAME ---"
echo "Team: ${TEAM_NAME}"
echo "Image: ${IMAGE_PATH}"
echo "Size: ${IMAGE_SIZE_GB} GB"
