import argparse
import requirements
import requests
import json
import re
import functools

from pprint import pprint

from pip._internal.req import parse_requirements as _parse_requirements
from pip._vendor.packaging import specifiers


requirement_matcher = re.compile(r"(.*)(==|~=)(.*)")
trove_matcher = re.compile(r"(.*) (::) (.*) (::) (.*)")

def parse_requirements(filename):
    def split_requirements_string(string):
        match = requirement_matcher.match(string)
        requirement = dict()
        if match is not None :
            requirement["name"] = match[1]
            requirement["version_constraint"] = match[2]
            requirement["version"] = match[3]

        return requirement

    requirements = [split_requirements_string(req.requirement) for req in _parse_requirements(filename, None)]

    return requirements


def parse_trove(trove):
    match = trove_matcher.match(trove)
    if match is not None:
        return match

def trove_python_versions(trove):
    versions = list()
    for item in trove:
        match = parse_trove(item)
        if match is None:
            continue
        if len(match.groups()) == 5 and match[1] == "Programming Language":
            versions.append(match[5])
    return versions


@functools.lru_cache
def version_in_requires_python(requires_python, python_version):
    if requires_python is None:
        return False
    spec = specifiers.SpecifierSet(requires_python)
    return python_version in spec

class release_obj:

    def __init__(self, name, version):
        self.pypi_response = requests.get("https://pypi.org/pypi/{}/{}/json".format(name, version))
        self.version = self.pypi_response.json() if self.pypi_response.status_code == 200 else None

    def requires_python(self):
        return self.version["info"]["requires_python"]

    @property
    def version_specifier(self):
        return self.version["info"]["version"]

    def __lt__(self, comp):
        return self.version_specifier < comp

    def __gt__(self, comp):
        return self.version_specifier > comp

    def __eq__(self, comp):
        return self.version_specifier == comp

    def __hash__(self):
        return self.version_specifier.__hash__()

    def __str__(self):
        return self.version_specifier

    def __repr__(self):
        return self.__str__()

class package:

    def __init__(self, package, local_version):
        self.pypi_response = requests.get("https://pypi.org/pypi/{}/json".format(package))
        self.package = self.pypi_response.json() if self.pypi_response.status_code == 200 else None
        self.versions = dict()
        self.local_version = local_version

    @functools.cached_property
    def name(self):
        return self.package["info"]["name"]


    @functools.lru_cache
    def current_version(self):
        return self.package["info"]["version"]
    

    @functools.lru_cache
    def release(self, version):
        return self.package["releases"][version]


    @functools.lru_cache
    def releases(self, exclude_earlier_than_local=True):
        if len(self.versions) is not None:
            for version in self.package["releases"].keys():
                if version < self.local_version and exclude_earlier_than_local == True:
                    continue
                self.versions[version] = release_obj(self.name, version)
        return self.versions


    @functools.lru_cache
    def trove(self):
        return self.package["info"]["classifiers"]
    

    @functools.lru_cache
    def wheels_for_versions(self, version):
        pythons = []
        for p in self.package["releases"][version]:
            pythons.append(p["python_version"])
        return pythons


    @functools.lru_cache
    def wheel_for_version(self, python_version="3.8"):
        return python_version in self.wheels_for_versions(self.current_version())


    def trove_versions(self):
        return trove_python_versions(self.trove())

    @functools.lru_cache
    def requires_python_for_version(self, version=None):
        if not version:
            version = self.current_version
        return release_obj(self.name, version).version["info"]["requires_python"]

    @functools.lru_cache
    def requires_python_supported_versions(self):
        versions = dict()
        for version in self.releases():
            versions[version] = requires_python_for_version(version)
        return versions
    
    @functools.lru_cache
    def latest_version_requires_python_version(self, python_version="3.8"):
        return python_version in self.requires_python_for_version(self.current_version())

    @functools.lru_cache
    def requires_python_supports_version(self, python_version, release):
        if isinstance(release, str): 
            return version_in_requires_python(self.requires_python_for_version(release), python_version)
        else:
            return version_in_requires_python(release.requires_python(), python_version)

            

    def upgradeable(self,python_version="3.8"):
        """ First check if requires_python exists, if yes, check if both 3.8, and 2.7 are supported there.
            Next, check trove.
            Finally, chepk there there are wheels for both 2.7 and 3.8.

            If requires_python doesnt exist, check trove, then check wheel lists.

            If neither trove, nor requires python exist, just check if there are wheels for both.
        """

        valid_versions = dict()

        releases = [release_obj(self.name, r) for r in self.releases()]

        for release in releases:
            if release.version_specifier <= self.local_version:
                continue

            version_validity_info = dict()
            version_validity_info["score"] = 0

            if release.requires_python() is not None and len(release.requires_python()) > 0:
                if self.requires_python_supports_version(python_version, release) and self.requires_python_supports_version("2.7", release):
                    version_validity_info["requires_python_support"] = True
                    version_validity_info["score"] += 1
                if (python_version[0] in self.trove_versions() or python_version in self.trove_versions()) and "2.7" in self.trove_versions():
                    version_validity_info["trove_versions_support"] = True
                    version_validity_info["score"] += 1
                if (self.wheel_for_version(python_version=python_version) and self.wheel_for_version("2.7")) or (self.wheel_for_version(python_version="cp27") and self.wheel_for_version(python_version="cp38")) or self.wheel_for_version("py2.3") or self.wheel_for_version("py3.py3"):
                    version_validity_info["wheels_available"] = True
                    version_validity_info["score"] += 1

            if version_validity_info["score"] >= 1:
                valid_versions[release.version_specifier] = version_validity_info


        if len(valid_versions.keys()) == 0:
            return (False, None)

        return (True, valid_versions)

    def upgradeable_for_3_support(self, python_version="3.8"):
        """ Return packages where the current version doesnt support Python3, but newer ones do"""
        valid_versions = dict()

        releases = [release_obj(self.name, r) for r in self.releases()]
        
        if self.requires_python_supports_version("3.8", self.local_version) or ("3" in self.trove_versions() or "3.8" in self.trove_versions()):
            return (False, None)
        if self.requires_python_supports_version("3.7", self.local_version) or ("3" in self.trove_versions() or "3.8" in self.trove_versions()):
            return (False, None)
        if self.requires_python_supports_version("3.6", self.local_version) or ("3" in self.trove_versions() or "3.8" in self.trove_versions()):
            return (False, None)
        else:
            print("Current package doesnt support Python3.8, Checking if newer packages do.")
            upgradeable38 = self.upgradeable(python_version="3.8")
            upgradeable37 = self.upgradeable(python_version="3.7")
            upgradeable36 = self.upgradeable(python_version="3.6")

        if upgradeable38[0] or upgradeable37[0] or upgradeable36[0]:
            return (True, upgradeable38[1] + upgradeable37[1] + upgradeable36[1] )
        else:
            return (False, None)

    def upgradeable_for_any_3(self):
        upgradeable38 = self.upgradeable(python_version="3.8")
        upgradeable37 = self.upgradeable(python_version="3.7")
        upgradeable36 = self.upgradeable(python_version="3.6")

        valid_versions = upgradeable38 + upgradeable37 + upgradeable36

        if len(valid_versions.keys()) == 0:
            return (False, None)

        return (True, valid_versions)

    def gained_38_support(self):
        """ Generate a list of versions showing when the package gained 3.6, 3.7 and 3.8 support """

        valid_versions = dict()
        releases = sorted([release_obj(self.name, r) for r in self.releases(exclude_earlier_than_local=False)])
        
        checked_38 = False
        checked_37 = False
        checked_36 = False

        for r in releases:
            version_info = dict()
            if "3.8" in trove_python_versions(r.version["info"]["classifiers"]) and not checked_38:
                version_info["added_3.8"] = r.version_specifier
                checked_38 = True
            if "3.7" in trove_python_versions(r.version["info"]["classifiers"]) and not checked_37:
                version_info["added_3.7"] = r.version_specifier
                checked_37 = True
            if "3.6" in trove_python_versions(r.version["info"]["classifiers"]) and not checked_36:
                version_info["added_3.6"] = r.version_specifier
                checked_36 = True
            if len(version_info) > 0:
                valid_versions[r.version_specifier] = version_info
            if checked_38 and checked_37 and checked_36:
                break

        if len(valid_versions) == 0:
            return (False, None)
        return (True, valid_versions)


def main():



    parser = argparse.ArgumentParser()

    parser.add_argument("--req", nargs=1, action="store", required=True)
    parser.add_argument("--upgrade-for-3", action="store_true", required=False, help="Only list packages which dont currently support python38, but could if we upgraded")
    parser.add_argument("--upgrade-for-any-3", action="store_true", required=False, help="Packages where newer versions support 3.6, 3.7 or 3.8")
    parser.add_argument("--added_support", action="store_true", required=False, help="Packages where support for python3.8 was added after our local version")

    args = parser.parse_args()

    print("Checking requirements file: {}".format(args.req[0]))

    upgradeable = []

    for req in parse_requirements(args.req[0]):
        if len(req) == 0:
            continue
        print(req["name"], req["version_constraint"], req["version"])
        name = req["name"]
        version = req["version"] 
        if version != None:
            pypi_package = package(name, version)
            if pypi_package.package is None:
                print("Skipping {}".format(name))
                print(pypi_package.pypi_response.status_code)
                continue
            if pypi_package.current_version() == version:
                continue
            print("Name: {}, Current Version: {}".format(name, version))
            print("Top Version on PyPi: {}".format(pypi_package.current_version()))
            #print("Versions
            #print("Supports 3.8?: {}".format(pypi_package.supports_python_version("3.8")))
            print("Wheels available for: {}".format(pypi_package.wheels_for_versions(pypi_package.current_version())))
            print("Trove: {}".format(pypi_package.trove_versions()))
            print("Requires_python supports 3.8: {}".format(pypi_package.requires_python_supports_version("3.8", pypi_package.current_version())))


            if args.upgrade_for_3:
                is_upgradeable, valid_versions = pypi_package.upgradeable_for_3_support(version)
            elif args.upgrade_for_any_3:
                is_upgradeable, valid_versions = pypi_package.upgradeable_for_any_3()
            elif args.added_support:
                is_upgradeable, valid_versions = pypi_package.gained_38_support() # is_upgradeable should actually be 'added_support'
            else:
                is_upgradeable, valid_versions = pypi_package.upgradeable(version)

            if is_upgradeable:
                upgradeable.append((pypi_package, valid_versions))


    print("\n\nUpgradeable Packages:")
    for p, valid_versions in upgradeable:
        print("-" * 80)
        print("{}".format(p.name))
        print("Current Version: {}".format(p.local_version))
        print("Available Upgrade versions:")
        print(valid_versions)
        for v in valid_versions.keys():
            print("{}".format(v))
            pprint(valid_versions[v])

    with open("requirement_checker.out", "a") as out:
        for p, valid_versions in upgradeable:
            out.write("-" * 80)
            out.write("{}".format(p.name))
            out.write("Current Version: {}".format(p.local_version))
            out.write("Available Upgrade versions:")
            for v in valid_versions.keys():
                out.write("{}".format(v))
                pprint(valid_versions[v], stream=out)



if __name__ == "__main__":
    main()
