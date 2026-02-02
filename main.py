import re
import os
import typer
import inquirer
import jenkins
import time
import boto3
import json

from pprint import pprint
from prettytable import PrettyTable
from prettytable import TableStyle
from jsondiff import diff

from xml.etree import cElementTree as ElementTree

app = typer.Typer()

os.environ["PYTHONHTTPSVERIFY"] = "0"
aws_verify = False
aws_delete_changeset = True

class XmlListConfig(list):
    def __init__(self, aList):
        for element in aList:
            if element:
                # treat like dict
                if len(element) == 1 or element[0].tag != element[1].tag:
                    self.append(XmlDictConfig(element))
                # treat like list
                elif element[0].tag == element[1].tag:
                    self.append(XmlListConfig(element))
            elif element.text:
                text = element.text.strip()
                if text:
                    self.append(text)

class XmlDictConfig(dict):
    '''
    Example usage:

    >>> tree = ElementTree.parse('your_file.xml')
    >>> root = tree.getroot()
    >>> xmldict = XmlDictConfig(root)

    Or, if you want to use an XML string:

    >>> root = ElementTree.XML(xml_string)
    >>> xmldict = XmlDictConfig(root)

    And then use xmldict for what it is... a dict.
    '''
    def __init__(self, parent_element):
        if parent_element.items():
            self.update(dict(parent_element.items()))
        for element in parent_element:
            if element:
                # treat like dict - we assume that if the first two tags
                # in a series are different, then they are all different.
                if len(element) == 1 or element[0].tag != element[1].tag:
                    aDict = XmlDictConfig(element)
                # treat like list - we assume that if the first two tags
                # in a series are the same, then the rest are the same.
                else:
                    # here, we put the list in dictionary; the key is the
                    # tag name the list elements all share in common, and
                    # the value is the list itself 
                    aDict = {element[0].tag: XmlListConfig(element)}
                # if the tag has attributes, add those to the dict
                if element.items():
                    aDict.update(dict(element.items()))
                self.update({element.tag: aDict})
            # this assumes that if you've got an attribute in a tag,
            # you won't be having any text. This may or may not be a 
            # good idea -- time will tell. It works for the way we are
            # currently doing XML configuration files...
            elif element.items():
                self.update({element.tag: dict(element.items())})
            # finally, if there are no child tags and no attributes, extract
            # the text
            else:
                self.update({element.tag: element.text})

def get_job_parameters(server, jobName):
    my_job_xml = server.get_job_config(jobName)
    root = ElementTree.XML(my_job_xml)
    my_job = XmlDictConfig(root)
    properties = my_job['properties']['hudson.model.ParametersDefinitionProperty']['parameterDefinitions']['hudson.model.ChoiceParameterDefinition']

    accounts = [r['choices']['string'] for r in properties if r['name'] == 'account'][0]
    regions = [r['choices']['string'] for r in properties if r['name'] == 'region'][0]
    actions = [r['choices']['string'] for r in properties if r['name'] == 'action'][0]
    stacks = [r['choices']['string'] for r in properties if r['name'] == 'stackName'][0]

    questions = [
        inquirer.List('account', message='Account', choices=accounts),
        inquirer.List('region', message='Region', choices=regions),
        inquirer.List('action', message='Action', choices=actions),
        inquirer.Checkbox('stacks', message='Stacks', choices=stacks)
    ]

    answers = inquirer.prompt(questions)
    return answers

def get_queue_item(server, queue_item):
    queue_number = None
    item_queued = False
    run = 0

    while item_queued == False:
        output = server.get_queue_item(queue_item)

        if 'executable' in output and 'number' in output['executable']:
            item_queued = True
            queue_number = output['executable']['number']
        elif run >= 60:
            item_queued = True
        else:
            time.sleep(5)
            run = run + 1

    return queue_number

def get_console_output(server, jobName, queue_number):
    item_queued = False
    change_set = None
    run = 0

    while item_queued == False:
        build_info = server.get_build_info(jobName, queue_number)

        if 'result' in build_info:
            try:
                console_output = server.get_build_console_output(jobName, queue_number)

                with open("console/{}_{}.txt".format(jobName.replace('/', '_'), queue_number), "w") as f:
                    f.write(console_output)

                p = re.compile("\"Id\":\s+\"([^\"]*)\"")
                result = p.search(console_output)

                change_set = result.group(1)

                if len(change_set) > 0:
                    item_queued = True
                    print("Action succeed on change-set {}".format(change_set))
            except Exception as e:
                print('Unable to retrieve change-set id, waiting')
                time.sleep(5)
                run = run + 1
        elif run >= 60:
            item_queued = True
        else:
            time.sleep(5)
            run = run + 1
    
    return change_set

def run_job(server, jobName, account, action, region, stacks):
    for stack in stacks:
        print("Build Job for {} in account {} with region {} and stackName {}".format(action, account, region, stack))
        queue_item = server.build_job(jobName, parameters = {
            'account': account,
            'action': action,
            'region': region,
            'stackName': stack
        })
        print("Queue ID for Job: {}".format(queue_item))

        queue_number = get_queue_item(server, queue_item)
        if queue_number != None:
            print("Queue Number for Job: {}".format(queue_number))

            if action == 'update':
                print('Action is update - get console output')
                change_set = get_console_output(server, jobName, queue_number)

                if change_set != None:
                    print("Get Change-Set {}".format(change_set))
                    get_changes(change_set, region)
                else:
                    print("FAILURE! Update Action but not able to get Change Set from Console Output")
            
                print('Stopping Build after retrieving change set')
                server.stop_build(jobName, queue_number)
        else:
            print("FAILURE! Queue Number not found")

def get_job_config():
    questions = [
        inquirer.Text('server', message='Jenkins Server Url', default='https://hoth.corp.homesend.com/jenkins/'),
        inquirer.Text('jobName', message='Jenkins JobName', default='AWS/infra-tf-pins/develop'),
        inquirer.Text('username', message='Jenkins Username'),
        inquirer.Password('password', message='Jenkins Password')
    ]

    answers = inquirer.prompt(questions)
    return answers

def create_file_directories():
    if not os.path.exists('console'):
        os.makedirs('console')
    if not os.path.exists('changeset'):
        os.makedirs('changeset')

def get_change_set(change_set, region):
    client = boto3.client('cloudformation', region_name=region, verify=aws_verify)

    print("Get Change Set for Id {}".format(change_set))
    response = client.describe_change_set(
        ChangeSetName = change_set,
        IncludePropertyValues = True
    )

    stackName = response['StackName']
    changes = []
 
    for change in response['Changes']:
        resourceChange = change['ResourceChange']

        if 'ChangeSetId' in resourceChange:
            child_changes = get_change_set(resourceChange['ChangeSetId'], region)
            for change in child_changes:
                changes.append(change)
        else:
            change_object = {
                'StackName': stackName,
                'Action': resourceChange['Action'],
                'LogicalResourceId': resourceChange['LogicalResourceId'],
                'ResourceType': resourceChange['ResourceType'],
                'AfterContext': resourceChange['AfterContext'] if 'AfterContext' in resourceChange else None,
                'BeforeContext': resourceChange['BeforeContext'] if 'BeforeContext' in resourceChange else None
            }

            changes.append(change_object)

    return changes

def get_changes(change_set, region):
    client = boto3.client('cloudformation', region_name=region, verify=aws_verify)

    print("Get Status for Id {}".format(change_set))

    stackName = ''
    run = 0
    is_complete = False

    while is_complete == False:
        time.sleep(5)
        response = client.describe_change_set(
            ChangeSetName = change_set
        )

        if 'Status' in response:
            if response['Status'] == 'CREATE_COMPLETE':
                print('Chang Set completed')
                is_complete = True
            elif response['Status'] == 'FAILED':
                print('Change Set failed')
                is_complete = True
                return
            else:
                run = run + 1
        else:
            run = run + 1

        if run >= 60:
            is_complete = True
            return

    stackName = response['StackName']

    changes = get_change_set(change_set, region)

    table = PrettyTable(['Change', 'StackName', 'Logical Resource Id', 'Resource Type', 'Diff'])
    table.align = 'l'
    table.set_style(TableStyle.ORGMODE)

    for change in changes:
        jsondiff = ''

        if change['AfterContext'] is not None and change['BeforeContext'] is None:
            jsondiff = json.dumps(change['AfterContext'], indent=2)
        elif change['AfterContext'] is not None and change['BeforeContext'] is not None:
            jsondiff = diff(change['BeforeContext'], change['AfterContext'], load=True, dump=True)

        table.add_row([change['Action'], change['StackName'], change['LogicalResourceId'], change['ResourceType'], jsondiff.strip().replace('\\\"', '"')])

    print(table)

    with open("changeset/{}.txt".format(stackName), "w") as f:
        f.write(table.get_string())

    if aws_delete_changeset == True:
        client.delete_change_set(
            ChangeSetName = change_set
        )

@app.command('')
def run():
    job_config = get_job_config()

    server = jenkins.Jenkins(job_config['server'], username=job_config['username'], password=job_config['password'])
    user = server.get_whoami()
    version = server.get_version()
    print('Hello %s from Jenkins %s' % (user['fullName'], version))

    create_file_directories()

    answers = get_job_parameters(server, job_config['jobName'])
    run_job(server, job_config['jobName'], answers['account'], answers['action'], answers['region'], answers['stacks'])

@app.command()
def help():
    rprint("[yellow]run[yellow] [green]Run Job[green]")

if __name__ == '__main__':
    app() 
