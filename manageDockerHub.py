#!/usr/bin/env python3
# coding: UTF-8

import enum
import sys
import json
from tabulate import tabulate
from datetime import datetime
from datetime import date
from calendar import monthrange
import docker
import requests
from requests.auth import HTTPBasicAuth

# Text colors
R = "\033[0;31;40m" # red
G = "\033[0;32;40m" # green
N = "\033[0m" # reset

# Docker hub URL
dockerHubRegistryURL = "registry.hub.docker.com/pingidentity"

# Turn a string red for command line output
def colorStringRed(str):
    if str == None:
        return str
    return R + str + N

# Turn a string green for command line output
def colorStringGreen(str):
    if str == None:
        return str
    return G + str + N

# Chosen operation
class Operation(enum.Enum):
    help = 1
    list_images = 2
    list_tags = 3
    clean_tags = 4
    archive_tags = 5

# Filters for selection of tags
class FilterType(enum.Enum):
    tag_name = 1
    time_based = 2

class TagFilter:
    def __init__(self, json):
        self.imageName = json['imageName'].lower()
        try:
            type = json['type']
            self.type = FilterType[type.lower().replace("-","_")]
        except KeyError:
            exit("Invalid tag filter type: {}".format(type))
        if self.type == FilterType.tag_name:
            self.string = json['string'].lower()
        elif self.type == FilterType.time_based:
            # Parse a cutoff date from the value
            self.years = int(json['years'])
            self.months = int(json['months'])
            if self.months >= 12:
                self.years += self.months / 12
                self.months = self.months % 12
    
    # Check if the tag passes this filter. If it does pass, return a string with the reason why
    def filterTag(self, imageName, tag):
        # If this filter doesn't apply to the image, then exit early
        if self.imageName != "all" and self.imageName != None and self.imageName != imageName:
            return None

        if self.type == FilterType.tag_name:
            # Check if the tag name contains the string
            tagName = tag['name'].lower()
            if self.string in tagName:
                return "\"{}\" in tag name".format(self.string)
        elif self.type == FilterType.time_based:
            # Tag age check
            # Remove milliseconds and timezone from last_updated time to parse it
            lastUpdated = datetime.fromisoformat(tag['last_updated'][0 : tag['last_updated'].rindex('.')]).date()
            today = date.today()
            cutoffYear = today.year - self.years
            cutoffMonth = today.month - self.months
            if cutoffMonth <= 0:
                cutoffYear -= int(-cutoffMonth / 12) + 1
            cutoffMonth = cutoffMonth % 12
            if cutoffMonth == 0:
                cutoffMonth = 12
            cutoffDay = today.day
            # Make sure the day is valid for the given month (can be off by as much as 3 days when 
            # reducing from Feb 31->Feb 28, but I figure that it's not a big deal)
            if cutoffDay > monthrange(cutoffYear, cutoffMonth)[1]:
                cutoffDay = monthrange(cutoffYear, cutoffMonth)[1]

            cutoffDate = date(cutoffYear, cutoffMonth, cutoffDay)
            if lastUpdated < cutoffDate:
                return "older than the cutoff date of {}".format(cutoffDate)
        return None

    def __str__(self):
        if self.type == FilterType.tag_name:
            return "Tags with names containing the string \"{}\"".format(self.string)
        elif self.type == FilterType.time_based:
            return "Tags more than {} year(s) and {} month(s) old".format(self.years, self.months)
        raise ValueError("Invalid TagFilter type")


# Valid arguments for this script
allowed_boolean_args = ["--dry-run"]
allowed_string_args = ["--image-name", "--username", "--password", "--target-registry"]

# Parse command-line arguments
def parseArgs():
    foundArgs = {}
    # First arg should be the chosen operation
    if len(sys.argv) > 1:
        op = sys.argv[1].lower()
    else:
        op = "help"
    try:
        operation = Operation[op.replace("-","_")]
        foundArgs['--operation'] = operation
    except KeyError:
        exit("Invalid operation: {}".format(op))
    invalidTrailingArgs = None

    # Read argument values
    checkingForValue = False
    currentArg = None
    for index, arg in enumerate(sys.argv[2:]):
        if not checkingForValue:
            if arg.lower() in allowed_boolean_args:
                foundArgs[arg.lower()] = True
            elif arg.lower() in allowed_string_args:
                foundArgs[arg.lower()] = None
                checkingForValue = True
                currentArg = arg
            else:
                exit("Invalid arg: {}".format(arg))
        else:
            foundArgs[currentArg.lower()] = arg
            checkingForValue = False
            currentArg = None

    if checkingForValue:
        exit("No value provided for argument {}".format(currentArg))
    
    # Check for required arguments for different operations
    if operation == Operation.clean_tags and not foundArgs.get("--dry-run") and (not foundArgs.get("--username") or not foundArgs.get("--password")):
        exit("The --username and --password arguments must be provided for the clean-tags operation")

    if operation in [Operation.clean_tags, Operation.list_tags, Operation.archive_tags] and not foundArgs.get("--image-name"):
        message = "The --image-name argument must be provided for the {} operation".format(op)
        if operation != Operation.archive_tags:
            message += "\nUse '--image-name all' to refer to all ping images available on DockerHub"
        exit(message)

    if operation == Operation.archive_tags and not foundArgs.get("--target-registry"):
        exit("The --target-registry argument must be provided for the {} operation".format(op))

    return foundArgs

# Print the relevant error message and exit.
def exit(errorMessage):
    sys.stderr.write("❌ Error: %s\n" % errorMessage)
    sys.stderr.write("Run 'manageDockerHub.py help' for more information\n")
    sys.exit(1)

# Print a description of the tool
def printHelp():
    print("""
Usage: manageDockerHub.py OPERATION {options}
   where OPERATION in:
        help                        print this usage information

        list-images                 list all pingidentity images available on dockerhub

        list-tags                   list all tags for a specific pingidentity image available
                                    on dockerhub. Use the --image-name argument to specify the
                                    image whose tags should be listed.

        clean-tags                  delete certain tags for a specific pingidentity image
                                    available on dockerhub. Use the --image-name argument to
                                    specify the image whose tags should be deleted. The
                                    tagDeletionCriteria.json file, in the same directory as
                                    this script defines rules for deletion. The tags that will
                                    be deleted will be printed before deletion to allow
                                    canceling the operation if necessary. Use the --dry-run
                                    argument to skip the tag deletion step.

        archive-tags                copy all tags from docker hub to a target registry. This
                                    process requires pulling any tags updated in the last two
                                    months for the specified image, and then tagging them in
                                    the new repository.
        
   where {options} include:
        --image-name                pingidentity image name. Use "all" to run the operation on
                                    all pingidentity images, except when running the
                                    archive-tags operation.

        --username                  docker hub username. Needed to run clean-tags.

        --password                  docker hub password. Needed to run clean-tags.

        --dry-run                   when specified, the clean-tags operation will skip the
                                    step of actually deleting the tags.

        --target-registry           where tags should be archived by the archive-tags operation

Examples:
    List all available images on the pingidentity dockerhub
    manageDockerHub.py list-images

    List all available tags for pingidentity/pingdirectory on dockerhub
    manageDockerHub.py list-tags --image-name pingdirectory

    Delete any outdated tags for pingdirectory on dockerhub
    manageDockerHub.py clean-tags --image-name pingdirectory --username user --password password
    """)

# Get all product images
def getAllImages():
    images = []
    next = "https://hub.docker.com/v2/repositories/pingidentity/"
    while next != None:
        with requests.get(next) as response:
            imageData = json.loads(response.text)
            images.extend([image['name'] for image in imageData["results"]])
            next = imageData['next']
    return images

# Get all tags for a product image
def getAllTags(imageName, tagFilter = None):
    imageTags = []
    next = "https://hub.docker.com/v2/repositories/pingidentity/{}/tags".format(imageName)
    while next != None:
        with requests.get(next) as response:
            tagData = json.loads(response.text)
            imageTags.extend(tagData["results"])
            next = tagData['next']
    for imageTag in imageTags:
        imageTag.update({ 'filterPassReason' : None })
        imageTag.update({ 'imageName' : imageName })
        if tagFilter:
            for filter in tagFilter:
                if not imageTag['filterPassReason']:
                    imageTag.update({ 'filterPassReason' : filter.filterTag(imageName, imageTag) })
    return imageTags

# Print a table of tags for a given product image
def printTagTable(tags, includeCauseForDeletion = False):
    tableRows = []
    for tag in tags:
        # Don't print full digest since it is so long. Include 10 characters after "sha256:".
        digest = "Unknown"
        if 'digest' in tag['images'][0]:
            digest = tag['images'][0]['digest'][:17]
        tagRow = [tag['imageName'], tag['name'], digest, tag['last_updated']]
        if includeCauseForDeletion:
            tagRow.append(tag['filterPassReason'])
            if tag['filterPassReason']:
                tagRow = [colorStringRed(col) for col in tagRow]
            else:
                tagRow = [colorStringGreen(col) for col in tagRow]
        tableRows.append(tagRow)
    headerRow = ['Image', 'Tag', 'Digest', 'Last Updated']
    if includeCauseForDeletion:
        headerRow.append('Cause for Deletion')
    print (tabulate(tableRows, headers=headerRow))

# Delete image tags from the given list, returning tags that were successfully deleted
def deleteImageTags(tags, username, password):
    # Login to get a JWT authentication token
    loginResponse = requests.post("https://hub.docker.com/v2/users/login/", data={"username":username, "password":password})
    if loginResponse.status_code != 200:
        print("Failed to authenticate to the Docker Hub API. Response code {}. Response text: {}".format(loginResponse.status_code, loginResponse.text))
        return []
    responseJson = json.loads(loginResponse.text)
    authHeader = {"Authorization": "JWT {}".format(responseJson['token'])}
    successfullyDeleted = []
    for tag in tags:
        deleteUrl = "https://hub.docker.com/v2/repositories/pingidentity/{}/tags".format(tag['imageName'])
        # Build URL to delete the one specific tag
        tagDeleteUrl = "{}/{}/".format(deleteUrl, tag['name'])
        deleteResponse = requests.delete(tagDeleteUrl, headers=authHeader)
        if deleteResponse.status_code == 204:
            successfullyDeleted.append(tag)
        else:
            print("Failed to delete tag {}. Response code {}. Response text: {}".format(tag['name'], deleteResponse.status_code, deleteResponse.text))
    # Logout from API
    logoutResponse = requests.post("https://hub.docker.com/v2/logout/", headers=authHeader)
    if logoutResponse.status_code != 200:
        print("Failed to logout from Docker Hub API. Response code {}. Response text: {}".format(logoutResponse.status_code, logoutResponse.text))
    return successfullyDeleted

def pullAll(imageName):
    global client
    try:
        client.ping()
    except:
        exit("Failed to connect to local docker daemon. Ensure docker is running and docker environment variables are valid.")
    return client.images.pull(dockerHubRegistryURL + "/" + imageName, all_tags=True)

# Returns a dictionary of images, with keys being image short IDs and values being the docker SDK image
def pullTags(tags):
    global client
    try:
        client.ping()
    except:
        exit("Failed to connect to local docker daemon. Ensure docker is running and docker environment variables are valid.")
    result = {}
    for tag in tags:
        print("Pulling tag {} for image {}".format(tag['name'], tag['imageName']))
        image = client.images.pull(dockerHubRegistryURL + "/" + tag['imageName'], tag=tag['name'])
        result[image.short_id] = image
    return result

def tagAndPushAll(imagesByShortId, targetRegistry):
    anyTagsFailedPush = False
    for shortId in imagesByShortId:
        image = imagesByShortId[shortId]
        if len(image.tags) > 0:
            pushedTags = []
            failedTags = []
            for tag in image.tags:
                # Only push tags pulled from docker hub
                if dockerHubRegistryURL in tag:
                    print("Starting push for tag {}".format(tag))
                    # Remove the registry URL, pingidentity, and the existing tag name, leaving only the product name
                    registry = tag.replace(dockerHubRegistryURL, targetRegistry)
                    tagName = registry.split(':')[1]
                    registry = registry.split(':')[0]
                    image.tag(registry, tagName)
                    # Print progress as tags are pushed
                    lastLineLength = 0
                    errorEncountered = False
                    for line in client.images.push(registry, tagName, stream=True, decode=True):
                        if 'status' in line:
                            if line['status'] == 'Pushing':
                                # Overwrite previous line first to show progress bar
                                if lastLineLength:
                                    print(' '*lastLineLength, end='\r', flush=True)
                                print(line['progress'], end='\r', flush=True)
                                lastLineLength = len(line['progress'])
                            elif line['status'] in ['Pushed', 'Layer already exists']:
                                if lastLineLength:
                                    print(' '*lastLineLength, end='\r', flush=True)
                                print("{}. id: {}".format(line['status'], line['id']))
                        elif 'error' in line:
                            if lastLineLength:
                                print(' '*lastLineLength, end='\r', flush=True)
                            print(colorStringRed("Error: {}".format(line['error'])))
                            errorEncountered = True

                    if errorEncountered:
                        failedTags.append(tag)
                        anyTagsFailedPush = True
                    else:
                        pushedTags.append(tag)
            # Print list of tags that were pushed for the image
            print()
            if pushedTags:
                print("The following tags for image {} have been pushed to registry {}:".format(image.short_id, targetRegistry))
                print('\n'.join(pushedTags))
            if failedTags:
                print("❌ The following tags for image {} could not be pushed to registry {} due to an error:".format(image.short_id, targetRegistry))
                print('\n'.join(failedTags))
            if not pushedTags and not failedTags:
                print("No tags pushed for image {}".format(image.short_id))
            print()
    return not anyTagsFailedPush

# Prompt for a yes or no response on the command-line
# https://stackoverflow.com/questions/3041986/apt-command-line-interface-like-yes-no-input
def promptYesNo(prompt):
    valid = {"yes": True, "y": True, "ye": True,
             "no": False, "n": False}
    # Default to No
    while True:
        sys.stdout.write("{} [y/N] ".format(prompt))
        choice = input().lower()
        if choice == '':
            return False
        elif choice in valid:
            return valid[choice]
        else:
            sys.stdout.write("Please respond with 'yes' or 'no' (or 'y' or 'n').\n")

# Prompt for a yes or no, and exit if no is selected
def promptYesOrExit(prompt):
    if not promptYesNo(prompt):
        print("Exiting")
        sys.exit(0)

# Main script processing
args = parseArgs()

operation = args.get("--operation")
imageName = args.get("--image-name")
dryRun = args.get("--dry-run")
username = args.get("--username")
password = args.get("--password")
targetRegistry = args.get("--target-registry")

images = [imageName]
if imageName == "all" or operation == Operation.list_images:
    print("Getting image names...")
    images = getAllImages()

if operation == Operation.help:
    printHelp()
elif operation == Operation.list_images:
    print('\n'.join(images))
elif operation == Operation.list_tags:
    imageTags = []
    for imageName in images:
        print("Getting image tags for {}...".format(imageName))
        imageTags.extend(getAllTags(imageName))
    printTagTable(imageTags)
elif operation == Operation.clean_tags:
    tagDeletionCriteria = []
    with open('tagDeletionCriteria.json') as f:
        tagDeletionJson = json.load(f)
    for criteria in tagDeletionJson:
        criterionObject = TagFilter(criteria)
        name = criterionObject.imageName
        if name == "all" or name in images:
            tagDeletionCriteria.append(criterionObject)

    print("The criteria for tag deletion are:")
    for criterionObj in tagDeletionCriteria:
        if criterionObj.imageName == "all":
            message = print("For all images: {}".format(criterionObj))
        else:
            message = print("For image {}: {}".format(criterionObj.imageName, criterionObj))

    promptYesOrExit("\nContinue? You will have a chance to review the selected tags before they are deleted.")

    imageTags = []
    for imageName in images:
        print("Getting image tags for {}...".format(imageName))
        imageTags.extend(getAllTags(imageName, tagDeletionCriteria))
    imageTagsToDelete = [imageTag for imageTag in imageTags if imageTag['filterPassReason'] != None]
    if not imageTagsToDelete:
        print("No images found that should be deleted based on the given deletion criteria")
        sys.exit(0)

    print("The following tags will be deleted:")
    printTagTable(imageTagsToDelete, True)

    if dryRun:
        print("Exiting since the --dry-run argument was provided. Run the script again without --dry-run to delete the tags")
        sys.exit(0)
    promptYesOrExit("\nAre you sure you want to permanently delete these tags from Docker Hub?")
    
    print("Deleting tags...")
    successfullyDeleted = deleteImageTags(imageTagsToDelete, username, password)
    print("The following tags were successfully deleted:")
    printTagTable(successfullyDeleted, True)
elif operation == Operation.archive_tags:
    print("Determining which tags to pull for image {}...".format(imageName))
    # Pull any image tags updated within the last 2 months (to be safe, in case of a late/early sprint release cycle)
    tagFilter = TagFilter(json.loads('{"imageName": "all", "type": "time-based", "years": "0", "months": "2"}'))
    imageTags = getAllTags(imageName, [tagFilter])
    # Filter out tags that were not modified in the last 2 months
    tagsToPull = [imageTag for imageTag in imageTags if not imageTag['filterPassReason']]

    client = docker.from_env()
    print("Pulling tags for image {}. This may take a while...".format(imageName))
    pulledImages = pullTags(tagsToPull)

    # Print table of tags that will be pushed
    tableRows = []
    for shortId in pulledImages:
        for tag in pulledImages[shortId].tags:
            if dockerHubRegistryURL in tag:
                tagRow = [tag, shortId]
                tableRows.append(tagRow)

    if len(tableRows) == 0:
        print("No tags found that need to be archived for {}".format(imageName))
        sys.exit(0)

    headerRow = ['Tag', 'ID']
    print (tabulate(tableRows, headers=headerRow))
    promptYesOrExit("The above images will be pushed to the target registry {}. Continue?".format(targetRegistry))

    print("Pull completed. Tagging for the target registry...")
    if tagAndPushAll(pulledImages, targetRegistry):
        print(colorStringGreen("All {} images tagged and pushed to the target registry!".format(imageName)))
    else:
        print("❌ One or more {} image tags could not be pushed to the target registry".format(imageName))
