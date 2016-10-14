import os
import sys
import urllib2
import shutil
import subprocess
import re
import hashlib

# The release channel name
channel = sys.argv[1]

# The archive date for the final 'rust' release artifacts,
# as determined by rust-buildbot
today = sys.argv[2]

# The s3 http address to use for querying info about packages.  Use
# this instead of public_addy because it's not behind cloudfront,
# which sometimes disagrees with the s3 bucket.
#
# Among other things, this addressed is used to retrieve the hashes
# of the components, which are then embedded in the manifest.
#
# FIXME: There is a security problem here, with using http to build
# the manifests. It may be lessened by the build master running on
# aws. So amazon is routing our traffic, and they are not going to
# mitm us.
s3_addy = sys.argv[3]

# The live address at which packages will be downloaded,
# i.e. https://static.rust-lang.org. This is the public
# version of s3_addy, for encoding in manifests.
public_addy = sys.argv[4]

# The directory containing the 'rust' packages as output
# by rust-packaging. These have been previously generated
# by rust-buildbot, but not yet uploaded to s3.
#
# This is also the directory we'll write the manifest to,
# or in the case of stable, two manifests.
rust_package_dir = sys.argv[5]

# Temporary work directory
temp_dir = sys.argv[6]

print "channel: " + channel
print "today: " + today
print "s3_addy: " + s3_addy
print "public_addy: " + public_addy
print "rust_package_dir: " + rust_package_dir
print "temp_dir: " + temp_dir
print

# These are the platforms we produce compilers for
host_list = sorted([
    "aarch64-unknown-linux-gnu",
    "arm-unknown-linux-gnueabi",
    "arm-unknown-linux-gnueabihf",
    "armv7-unknown-linux-gnueabihf",
    "i686-apple-darwin",
    "i686-pc-windows-gnu",
    "i686-pc-windows-msvc",
    "i686-unknown-linux-gnu",
    # "mips-unknown-linux-gnu",
    # "mipsel-unknown-linux-gnu",
    # "mips64-unknown-linux-gnuabi64",
    # "mips64el-unknown-linux-gnuabi64",
    # "powerpc-unknown-linux-gnu",
    # "powerpc64-unknown-linux-gnu",
    # "powerpc64le-unknown-linux-gnu",
    # "s390x-unknown-linux-gnu",
    "x86_64-apple-darwin",
    "x86_64-pc-windows-gnu",
    "x86_64-pc-windows-msvc",
    "x86_64-unknown-freebsd",
    "x86_64-unknown-linux-gnu",
    "x86_64-unknown-netbsd",
])

# These are the platforms we produce standard libraries for
target_list = sorted([
    "aarch64-apple-ios",
    "aarch64-linux-android",
    "aarch64-unknown-linux-gnu",
    "arm-linux-androideabi",
    "arm-unknown-linux-gnueabi",
    "arm-unknown-linux-gnueabihf",
    "arm-unknown-linux-musleabi",
    "arm-unknown-linux-musleabihf",
    "armv7-apple-ios",
    "armv7-linux-androideabi",
    "armv7-unknown-linux-gnueabihf",
    "armv7-unknown-linux-musleabihf",
    "armv7s-apple-ios",
    "asmjs-unknown-emscripten",
    "i386-apple-ios",
    "i586-pc-windows-msvc",
    "i586-unknown-linux-gnu",
    "i686-apple-darwin",
    "i686-linux-android",
    "i686-pc-windows-gnu",
    "i686-pc-windows-msvc",
    "i686-unknown-freebsd",
    "i686-unknown-linux-gnu",
    "i686-unknown-linux-musl",
    "mips-unknown-linux-gnu",
    "mips-unknown-linux-musl",
    "mips64-unknown-linux-gnuabi64",
    "mips64el-unknown-linux-gnuabi64",
    "mipsel-unknown-linux-gnu",
    "mipsel-unknown-linux-musl",
    "powerpc-unknown-linux-gnu",
    "powerpc64-unknown-linux-gnu",
    "powerpc64le-unknown-linux-gnu",
    "s390x-unknown-linux-gnu",
    "wasm32-unknown-emscripten",
    "x86_64-apple-darwin",
    "x86_64-apple-ios",
    "x86_64-pc-windows-gnu",
    "x86_64-pc-windows-msvc",
    "x86_64-rumprun-netbsd",
    "x86_64-unknown-freebsd",
    "x86_64-unknown-linux-gnu",
    "x86_64-unknown-linux-musl",
    "x86_64-unknown-netbsd",
])

# windows-gnu platforms require an extra bundle of gnu stuff
mingw_list = sorted([
    "x86_64-pc-windows-gnu",
    "i686-pc-windows-gnu",
])

# This file defines which cargos go with which rustc on beta/stable
cargo_revs = "https://raw.githubusercontent.com/rust-lang/rust-packaging/master/cargo-revs.txt"

if os.path.isdir(temp_dir):
    shutil.rmtree(temp_dir)
os.mkdir(temp_dir)

def main():
    # First figure out the distribution archive dates for the rustc
    # we're interested in and its associated cargo

    # Get the archive date from the channel information generated by
    # rust-buildbout
    rustc_date = most_recent_build_date(channel, "rustc")

    # Get the version of rustc by downloading it from s3 and
    # reading the `version` file
    rustc_version = version_from_channel(channel, "rustc", rustc_date)

    # The short version is just the 'major.minor.point' version string.
    # This is used for pairing with cargo and guessing file names
    # on s3.
    rustc_short_version = parse_short_version(rustc_version)

    cargo_date = None
    if channel == "nightly":
        # Nightly rust is paired with nightly cargo
        cargo_date = most_recent_build_date("nightly", "cargo")
    else:
        # Beta / stable rust is paired with a cargo specified
        # by rust-packaging
        cargo_date = cargo_date_from_packaging(rustc_short_version)

    cargo_version = version_from_channel("nightly", "cargo", cargo_date)
    cargo_short_version = parse_short_version(cargo_version)

    print "rustc date: " + rustc_date
    print "rustc version: " + rustc_version
    print "rustc short version: " + rustc_short_version
    print "cargo date: " + cargo_date
    print "cargo version: " + cargo_version
    print "cargo short version: " + cargo_short_version

    # Validate the component artifacts and generate the manifest
    generate_manifest(rustc_date, rustc_version, rustc_short_version,
                      cargo_date, cargo_version, cargo_short_version)

# Use the channel-$component-$channel-date.txt file to get the archive
# date for cargo or rustc. These files are generated by finish_dist
# in rust-buildbot.
def most_recent_build_date(channel, component):
    dist = dist_folder(component)
    date_url = s3_addy + "/" + dist + "/channel-" + component + "-" + channel + "-date.txt"
    print "downloading " + date_url
    response = urllib2.urlopen(date_url)
    if response.getcode() != 200:
        raise Exception("couldn't download " + date_url)
    date = response.read().strip();
    return date

# Read the v1 manifests to find the installer name, download it
# and extract the version file.
def version_from_channel(channel, component, date):
    dist = dist_folder(component)

    # Load the manifest
    manifest_url = s3_addy + "/" + dist + "/" + date + "/channel-" + component + "-" + channel
    print "downloading " + manifest_url
    response = urllib2.urlopen(manifest_url)
    if response.getcode() != 200:
        raise Exception("couldn't download " + manifest_url)
    manifest = response.read().strip();

    # Find the installer name
    installer_name = None
    for line in manifest.split("\n"):
        if line.startswith(component) and line.endswith(".tar.gz"):
            installer_name = line

    if installer_name == None:
        raise Exception("couldn't find installer in manifest for " + component)

    # Download the installer
    installer_url = s3_addy + "/" + dist + "/" + date + "/" + installer_name
    print "downloading " + installer_url
    response = urllib2.urlopen(installer_url)
    if response.getcode() != 200:
        raise Exception("couldn't download " + installer_url)

    installer_file = temp_dir + "/" + installer_name
    f = open(installer_file, "w")
    while True:
        buf = response.read(4096)
        if not buf: break
        f.write(buf)
    f.close()

    # Unpack the installer
    unpack_dir = temp_dir + "/unpack"
    os.mkdir(unpack_dir)
    r = subprocess.call(["tar", "xzf", installer_file, "-C", unpack_dir, "--strip-components", "1"])
    if r != 0:
        raise Exception("couldn't extract tarball " + installer_file)

    version = None
    version_file = unpack_dir + "/version"
    with open(version_file, 'r') as f:
        version = f.read().strip()

    shutil.rmtree(unpack_dir)
    os.remove(installer_file)

    return version

def dist_folder(component):
    if component == "cargo":
        return "cargo-dist"
    return "dist"

def cargo_date_from_packaging(rustc_version):
    print "downloading " + cargo_revs
    response = urllib2.urlopen(cargo_revs)
    if response.getcode() != 200:
        raise Exception("couldn't download " + cargo_revs)
    revs = response.read().strip()
    for line in revs.split("\n"):
        values = line.split(":")
        version = values[0].strip()
        date = values[1].strip()
        if version == rustc_version:
            return date
    raise Exception("couldn't find cargo rev for " + rustc_version)


def parse_short_version(version):
    p = re.compile("^\d*\.\d*\.\d*")
    m = p.match(version)
    if m is None:
        raise Exception("couldn't parse version: " + version)
    v = m.group(0)
    if v is None:
        raise Exception("couldn't parse version: " + version)
    return v

def generate_manifest(rustc_date, rustc_version, rustc_short_version,
                      cargo_date, cargo_version, cargo_short_version):

    m = build_manifest(rustc_date, rustc_version, rustc_short_version,
                       cargo_date, cargo_version, cargo_short_version)

    manifest_name = "channel-rust-" + channel + ".toml"
    manifest_file = rust_package_dir + "/" + manifest_name
    write_manifest(m, manifest_file)

    # Stable releases get a permanent manifest named after the version
    if channel == "stable":
        manifest_name = "channel-rust-" + rustc_short_version + ".toml"
        manifest_file = rust_package_dir + "/" + manifest_name
        write_manifest(m, manifest_file)

    print_summary(m)

def build_manifest(rustc_date, rustc_version, rustc_short_version,
                   cargo_date, cargo_version, cargo_short_version):

    packages = {}

    # Build all the non-rust packages. All the artifects here are
    # already in the archives and will be verified.
    rustc_pkg = build_package_def_from_archive("rustc", "dist", rustc_date,
                                               rustc_version, rustc_short_version,
                                               host_list)
    std_pkg = build_package_def_from_archive("rust-std", "dist", rustc_date,
                                             rustc_version, rustc_short_version,
                                             target_list)
    doc_pkg = build_package_def_from_archive("rust-docs", "dist", rustc_date,
                                             rustc_version, rustc_short_version,
                                             host_list)
    cargo_pkg = build_package_def_from_archive("cargo", "cargo-dist", cargo_date,
                                               cargo_version, cargo_short_version,
                                               host_list)
    mingw_pkg = build_package_def_from_archive("rust-mingw", "dist", rustc_date,
                                               rustc_version, rustc_short_version,
                                               mingw_list)
    src_pkg = build_package_def_from_archive("rust-src", "dist", rustc_date,
                                             rustc_version, rustc_short_version,
                                             ["*"])

    packages["rustc"] = rustc_pkg
    packages["rust-std"] = std_pkg
    packages["rust-docs"] = doc_pkg
    packages["cargo"] = cargo_pkg
    packages["rust-mingw"] = mingw_pkg
    packages["rust-src"] = src_pkg

    # Build the rust package. It is the only one with subcomponents
    rust_target_pkgs = {}
    for host in host_list:
        required_components = []
        extensions = []

        for component in ["rustc", "rust-std", "rust-docs", "cargo"]:
            required_components += [{
                "pkg": component,
                "target": host,
            }]

        if "windows" in host and "gnu" in host:
            required_components += [
                {
                    "pkg": "rust-mingw",
                    "target": host,
                }
            ]

        # All std are extensions
        for target in target_list:
            # host std is required though
            if target == host: continue

            extensions += [{
                "pkg": "rust-std",
                "target": target,
            }]

        # The src package is also an extension
        extensions += [{
            "pkg": "rust-src",
            "target": "*",
        }]

        # The binaries of the 'rust' package are on the local disk.
        # url_and_hash_of_rust_package will try to locate them
        # and tell us where they are going to live on static.rust-lang.org
        available = False
        url = ""
        hash = ""

        url_and_hash = url_and_hash_of_rust_package(host, rustc_short_version)
        if url_and_hash != None:
            available = True
            url = url_and_hash["url"]
            hash = url_and_hash["hash"]

        rust_target_pkgs[host] = {
            "available": available,
            "url": url,
            "hash": hash,
            "components": required_components,
            "extensions": extensions
        }

    packages["rust"] = {
        "version": rustc_version,
        "target": rust_target_pkgs,
    }

    return {
        "manifest-version": "2",
        "date": today,
        "pkg": packages,
    }

# Builds the definition of a single package, with all its targets,
# from the archives.
def build_package_def_from_archive(name, dist_dir, archive_date,
                                   version, short_version, target_list):
    target_pkgs = {}
    for target in target_list:
        url = live_package_url(name, dist_dir, archive_date, short_version, target)
        if url is not None:
            target_pkgs[target] = {
                "available": True,
                "url": url.replace(s3_addy, public_addy),
                "hash": hash_from_s3_installer(url),
            }
        else:
            print "error: " + name + " for " + target + " not available"
            target_pkgs[target] = {
                "available": False,
                "url": "",
                "hash": ""
            }

    return {
        "version": version,
        "target": target_pkgs
    }

# Find the URL of a package's installer tarball and return it or None if
# the package doesn't exist on s3.
#
# NB: This method does not know how the installer is named: whether like
# "rustc-nightly-$triple.tar.gz" or "rustc-$version-$triple.tar.gz". So
# it will try both.
def live_package_url(name, dist_dir, date, version, target):
    # Cargo builds are always named 'nightly'
    maybe_channel = channel
    if name == "cargo":
        maybe_channel = "nightly"

    if name == "rust-src":
        # The build system treats source packages as a separate target for `rustc`
        # but for rustup we'd like to treat them as a completely separate package.
        url1 = s3_addy + "/" + dist_dir + "/" + date + "/rust-src-" + version + ".tar.gz"
        url2 = s3_addy + "/" + dist_dir + "/" + date + "/rust-src-" + maybe_channel + ".tar.gz"
    else:
        url1 = s3_addy + "/" + dist_dir + "/" + date + "/" + name + "-" + version + "-" + target + ".tar.gz"
        url2 = s3_addy + "/" + dist_dir + "/" + date + "/" + name + "-" + maybe_channel + "-" + target + ".tar.gz"

    print "checking " + url1
    request = urllib2.Request(url1)
    request.get_method = lambda: "HEAD"

    try:
        response = urllib2.urlopen(request)
        if response.getcode() == 200:
            return url1
    except:
        pass

    print "checking " + url2
    request = urllib2.Request(url2)
    request.get_method = lambda: "HEAD"

    try:
        response = urllib2.urlopen(request)
        if response.getcode() == 200:
            return url2
    except:
        pass

    return None

# Finds the hash for an installer on s3 by downloading its sha256 file
def hash_from_s3_installer(url):
    hash_url = url + ".sha256"
    print "retreiving hash from " + hash_url
    response = urllib2.urlopen(hash_url)
    if response.getcode() != 200:
        raise Exception("expected hash not found at " + hash_url)

    hash_file_str = response.read()
    hash = hash_file_str[0:64]

    return hash

# Gets the url and hash of the 'rust' package for a target. These packages
# have not been uploaded to s3, but instead are on the local disk.
def url_and_hash_of_rust_package(target, rustc_short_version):
    version = channel
    if channel == "stable": version = rustc_short_version
    file_name = "rust-" + version + "-" + target + ".tar.gz"
    url = public_addy + "/dist/" + today + "/" + file_name
    local_file = rust_package_dir + "/" + file_name
    if not os.path.exists(local_file):
        print "error: rust package missing: " + local_file
        return None

    hash = None
    with open(local_file, 'rb') as f:
        buf = f.read()
        hash = hashlib.sha256(buf).hexdigest()

    return {
        "url": url,
        "hash": hash,
    }

def write_manifest(manifest, file_path):
    def quote(value):
        return '"' + str(value).replace('"', r'\"') + '"'

    def bare_key(key):
        if re.match(r"^[a-zA-Z0-9_\-]+$", key):
            return key
        else:
            return quote(key)

    with open(file_path, "w") as f:
        f.write('manifest-version = "2"\n')
        f.write('date = ' + quote(today) + '\n')
        f.write('\n')

        for name, pkg in sorted(manifest["pkg"].items()):
            f.write('[pkg.' + bare_key(name) + ']\n')
            f.write('version = ' + quote(pkg["version"]) + '\n')
            f.write('\n')

            for target, target_pkg in sorted(pkg["target"].items()):
                available = "true"
                if not target_pkg["available"]: available = "false"

                f.write('[pkg.' + bare_key(name) + '.target.' + bare_key(target) + ']\n')
                f.write('available = ' + available + '\n')
                f.write('url = ' + quote(target_pkg["url"]) + '\n')
                f.write('hash = ' + quote(target_pkg["hash"]) + '\n')
                f.write('\n')

                components = target_pkg.get("components")
                if components:
                    for component in components:
                        f.write('[[pkg.' + bare_key(name) + '.target.' + bare_key(target) + '.components]]\n')
                        f.write('pkg = ' + quote(component["pkg"]) + '\n')
                        f.write('target = ' + quote(component["target"]) + '\n')

                extensions = target_pkg.get("extensions")
                if extensions:
                    for extension in extensions:
                        f.write('[[pkg.' + bare_key(name) + '.target.' + bare_key(target) + '.extensions]]\n')
                        f.write('pkg = ' + quote(extension["pkg"]) + '\n')
                        f.write('target = ' + quote(extension["target"]) + '\n')

                f.write('\n')

def print_summary(manifest):

    print
    print "summary:"
    print

    for target, pkg in sorted(manifest["pkg"]["rust"]["target"].items()):
        if pkg["available"]:
            print "rust packaged for " + target
        else:
            print "rust *not* packaged for " + target

    for target, pkg in sorted(manifest["pkg"]["rust-std"]["target"].items()):
        if pkg["available"]:
            print "rust-std packaged for " + target
        else:
            print "rust-std *not* packaged for " + target

    print

main()
