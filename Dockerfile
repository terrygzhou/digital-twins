# digital-twins reference image (0.9.0)
# Base: python:3.11-slim (image-reference form — passes the portability guard;
#       matches the package's requires-python >= 3.11).
# Install: PEP 517 hatchling build via pip install .
# Expose: digital-twins console script.
# Host-neutral: no host paths, no python3.N host-pin shorthand.

FROM python:3.11-slim

# Install build dependencies for any compiled wheels, then remove them.
RUN pip install --no-cache-dir --upgrade pip

# Copy the package source and install via the hatchling build backend.
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

# The package's state directory (KB_STATE_DIR default resolves here at runtime;
# the config layer sets the actual path — this is just a writable volume mount
# point, not a host path).
VOLUME ["/data"]

# Expose the digital-twins console script (installed by pip install .).
ENTRYPOINT ["digital-twins"]
CMD ["--help"]
