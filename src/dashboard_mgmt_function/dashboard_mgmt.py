# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Manages create/update/delete of the Personalize Monitor CloudWatch dashboard

This function is called two ways:

1. From CloudFormation when the application is deployed, updated, or deleted in an AWS
account. When the resource is created, this function will create the Personalize
Monitor Dashboard in CloudWatch populated with widgets for monitoring Personalize
resources configured as deployment parameters.

When this resource is updated (i.e. redeployed), the dashboard will be rebuilt and
updated/replaced.

When this resource is deleted, this function will delete the CloudWatch Dashboard.

2. As the target of an EventBridge rule that signals that the dashboard should be
rebuilt as a result of an event occurring. The event could be after a campaign has
been deleted and therefore a good point to rebuild the dashboard. It could also
be setup to periodically rebuild the dashboard on a schedule so it picks up new
campaigns too.

See the layer_dashboard Lambda Laye for details on how the dashboard is built.
"""

import json
import os
import boto3
import chevron

from crhelper import CfnResource
from aws_lambda_powertools import Logger
from common import (
    extract_region,
    extract_account_id,
    get_client,
    get_configured_active_campaigns,
    get_configured_active_recommenders
)

logger = Logger()
helper = CfnResource()

cloudwatch = boto3.client('cloudwatch')

DASHBOARD_NAME = 'Amazon-Personalize-Monitor'

def build_dashboard(event):
    # Will hold the data used to render the template.
    template_data = {}

    template_data['namespace'] = 'PersonalizeMonitor'
    template_data['current_region'] = os.environ['AWS_REGION']

    logger.debug('Loading active campaigns and recommenders')

    campaigns = get_configured_active_campaigns(event)
    template_data['active_campaign_count'] = len(campaigns)

    recommenders = get_configured_active_recommenders(event)
    template_data['active_recommender_count'] = len(recommenders)

    # Group campaigns/recommenders by dataset group so we can create DSG specific widgets in rows
    resources_by_dsg_arn = {}
    # Holds DSG info so we only have describe once per DSG
    dsgs_by_arn = {}

    for campaign in campaigns:
        logger.info('Campaign %s will be added to the dashboard', campaign['campaignArn'])

        campaign_region = extract_region(campaign['campaignArn'])

        personalize = get_client('personalize', campaign_region)

        response = personalize.describe_solution_version(solutionVersionArn = campaign['solutionVersionArn'])

        dsg_arn = response['solutionVersion']['datasetGroupArn']
        recipe_arn = response['solutionVersion']['recipeArn']

        dsg = dsgs_by_arn.get(dsg_arn)
        if not dsg:
            response = personalize.describe_dataset_group(datasetGroupArn = dsg_arn)
            dsg = response['datasetGroup']
            dsgs_by_arn[dsg_arn] = dsg

        inference_resource_datas = resources_by_dsg_arn.get(dsg_arn)
        if not inference_resource_datas:
            inference_resource_datas = []
            resources_by_dsg_arn[dsg_arn] = inference_resource_datas

        campaign_data = {
            'name': campaign['name'],
            'resource_arn_name': 'CampaignArn',
            'resource_min_tps_name': 'minProvisionedTPS',
            'resource_avg_tps_name': 'averageTPS',
            'resource_utilization_name': 'campaignUtilization',
            'inference_arn': campaign['campaignArn'],
            'region': campaign_region
        }

        if recipe_arn == 'arn:aws:personalize:::recipe/aws-personalized-ranking':
            campaign_data['latency_metric_name'] = 'GetPersonalizedRankingLatency'
        else:
            campaign_data['latency_metric_name'] = 'GetRecommendationsLatency'

        inference_resource_datas.append(campaign_data)

    for recommender in recommenders:
        logger.info('Recommender %s will be added to the dashboard', recommender['recommenderArn'])

        recommender_region = extract_region(recommender['recommenderArn'])

        dsg_arn = recommender['datasetGroupArn']

        dsg = dsgs_by_arn.get(dsg_arn)
        if not dsg:
            response = personalize.describe_dataset_group(datasetGroupArn = dsg_arn)
            dsg = response['datasetGroup']
            dsgs_by_arn[dsg_arn] = dsg

        inference_resource_datas = resources_by_dsg_arn.get(dsg_arn)
        if not inference_resource_datas:
            inference_resource_datas = []
            resources_by_dsg_arn[dsg_arn] = inference_resource_datas

        recommender_data = {
            'name': recommender['name'],
            'resource_arn_name': 'RecommenderArn',
            'resource_min_tps_name': 'minRecommendationRequestsPerSecond',
            'resource_avg_tps_name': 'averageRPS',
            'resource_utilization_name': 'recommenderUtilization',
            'latency_metric_name': 'GetRecommendationsLatency',
            'inference_arn': recommender['recommenderArn'],
            'region': recommender_region
        }

        inference_resource_datas.append(recommender_data)

    dsgs_for_template = []

    for dsg_arn, inference_resource_datas in resources_by_dsg_arn.items():
        dsg = dsgs_by_arn[dsg_arn]

        # Minor hack to know when we're on the last item in list when iterating in template.
        inference_resource_datas[len(inference_resource_datas) - 1]['last_resource'] = True

        dsgs_for_template.append({
            'name': dsg['name'],
            'region': extract_region(dsg_arn),
            'account_id': extract_account_id(dsg_arn),
            'inference_resources': inference_resource_datas
        })

    template_data['dataset_groups'] = sorted(dsgs_for_template, key = lambda dsg: dsg['region'] + dsg['name'])

    # Render template and use as dashboard body.
    with open('dashboard-template.mustache', 'r') as f:
        dashboard = chevron.render(f, template_data)

        logger.debug(json.dumps(dashboard, indent = 2, default = str))

        logger.info('Adding/updating dashboard')

        cloudwatch.put_dashboard(
            DashboardName = DASHBOARD_NAME,
            DashboardBody = dashboard
        )

def delete_dashboard():
    logger.info('Deleting dashboard')

    cloudwatch.delete_dashboards(
        DashboardNames = [ DASHBOARD_NAME ]
    )

@helper.create
@helper.update
def create_or_update_resource(event, _):
    build_dashboard(event)

@helper.delete
def delete_resource(event, _):
    delete_dashboard()

@logger.inject_lambda_context(log_event=True)
def lambda_handler(event, context):
    # If the event has a RequestType, we're being called by CFN as custom resource
    if event.get('RequestType'):
        logger.info('Called via CloudFormation as a custom resource; letting CfnResource route request')
        helper(event, context)
    else:
        logger.info('Called via Invoke; assuming caller wants to build dashboard')

        if event.get('detail'):
            reason = event['detail'].get('Reason')
        else:
            reason = event.get('Reason')

        if reason:
            logger.info('Reason for dashboard build: %s', reason)

        build_dashboard(event)