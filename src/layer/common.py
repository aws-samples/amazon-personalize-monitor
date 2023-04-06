# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda layer functions shared across Lambda functions in this application
"""

import boto3
import os
import json
import logging
import random
from typing import Dict, List

from botocore.exceptions import ClientError
from aws_lambda_powertools import Logger
from expiring_dict import ExpiringDict

logger = Logger(child=True)

_clients_by_region = {}
# Since the DescribeCampaign and DescribeRecommender APIs easily throttle,
# use a cache to help smooth out periods where we get throttled.
_resource_cache = ExpiringDict(max_age_seconds = 22 * 60)

PROJECT_NAME = 'PersonalizeMonitor'
ALARM_NAME_PREFIX = PROJECT_NAME + '-'
SNS_TOPIC_NAME = 'PersonalizeMonitorNotifications'
NOTIFICATIONS_RULE = 'PersonalizeMonitor-NotificationsRule'
NOTIFICATIONS_RULE_TARGET_ID = 'PersonalizeMonitorNotificationsId'

def put_event(detail_type, detail, resources = []):
    event_bridge = get_client('events')

    logger.info({
        'detail_type': detail_type,
        'detail': detail,
        'resources': resources
    })

    event_bridge.put_events(
        Entries=[
            {
                'Source': 'personalize.monitor',
                'Resources': resources,
                'DetailType': detail_type,
                'Detail': detail
            }
        ]
    )

def extract_region(arn: str) -> str:
    ''' Extracts region from an AWS ARN '''
    region = None
    elements = arn.split(':')
    if len(elements) > 3:
        region = elements[3]

    return region

def extract_resource_type(arn: str) -> str:
    ''' Extracts resource type from an AWS ARN '''
    resource = None
    elements = arn.split(':')
    if len(elements) > 5:
        resource = elements[5].split('/')[0]

    return resource

def is_campaign(arn: str) -> bool:
    return extract_resource_type(arn) == 'campaign'

def is_recommender(arn: str) -> bool:
    return extract_resource_type(arn) == 'recommender'

def extract_account_id(arn: str) -> str:
    ''' Extracts account ID from an AWS ARN '''
    account_id = None
    elements = arn.split(':')
    if len(elements) > 4:
        account_id = elements[4]

    return account_id

def get_client(service_name: str, region_name: str = None):
    if not region_name:
        region_name = os.environ['AWS_REGION']

    ''' Returns boto3 client for a service and region '''
    clients_by_service = _clients_by_region.get(region_name)

    if not clients_by_service:
        clients_by_service = {}
        _clients_by_region[region_name] = clients_by_service

    client = clients_by_service.get(service_name)

    if not client:
        client = boto3.client(service_name = service_name, region_name = region_name)
        clients_by_service[service_name] = client

    return client

def determine_regions(event: Dict) -> List[str]:
    ''' Determines regions from function event or environment '''
    # Check event first (list of region names)
    regions = None
    if event:
        regions = event.get('Regions')

    if not regions:
        # Check environment variable next for list of region names as CSV
        regions = os.environ.get('Regions')

    if not regions:
        # Lastly, use current region from environment.
        regions = os.environ['AWS_REGION']

    if regions and isinstance(regions, str):
        regions = [exp.strip(' ') for exp in regions.split(',')]

    return regions

def _determine_arns(event: Dict, arn_param_name: str, arn_list_type: str) -> List[str]:
    ''' Determines Personalize campaign ARNs based on function event or environment '''

    # Check event first (list of ARNs)
    arns_spec = None
    if event:
        arns_spec = event.get(arn_param_name)

    if not arns_spec:
        # Check environment variable next for list of ARNs as CSV
        arns_spec = os.environ.get(arn_param_name)

    if not arns_spec:
        raise Exception(f'"{arn_param_name}" expression required in event or environment')

    if isinstance(arns_spec, str):
        arns_spec = [exp.strip(' ') for exp in arns_spec.split(',')]

    logger.debug('%s expression: %s', arn_param_name, arns_spec)

    # Look for magic value of "all" to mean all active campaigns/recommenders in configured region(s)
    if len(arns_spec) == 1 and arns_spec[0].lower() == 'all':
        logger.debug('Retrieving all active ARNs')
        arns = []

        # Determine regions we need to consider
        regions = determine_regions(event)
        logger.debug('Regions to scan for active resources: %s', regions)

        for region in regions:
            personalize = get_client(service_name = 'personalize', region_name = region)

            arns_for_region = 0

            resources_paginator = personalize.get_paginator(arn_list_type)
            for resources_page in resources_paginator.paginate():
                if resources_page.get('campaigns'):
                    for resource in resources_page['campaigns']:
                        arns.append(resource['campaignArn'])
                        arns_for_region += 1
                elif resources_page.get('recommenders'):
                    for resource in resources_page['recommenders']:
                        arns.append(resource['recommenderArn'])
                        arns_for_region += 1

            logger.debug('Region %s has %d resources', region, arns_for_region)
    else:
        arns = arns_spec

    return arns

def determine_campaign_arns(event: Dict) -> List[str]:
    ''' Determines Personalize campaign ARNs based on function event or environment '''
    return _determine_arns(event, 'CampaignARNs', 'list_campaigns')

def determine_recommender_arns(event: Dict) -> List[str]:
    ''' Determines Personalize recommender ARNs based on function event or environment '''
    return _determine_arns(event, 'RecommenderARNs', 'list_recommenders')

def get_configured_active_campaigns(event: Dict) -> List[Dict]:
    ''' Returns list of active campaigns as configured by function event and/or environment '''
    campaign_arns = determine_campaign_arns(event)

    # Shuffle the list of arns so we don't try to describe campaigns in the same order each
    # time and potentially use cached campaign details for the same campaigns further down
    # the list due to rare but possible API throttling.
    random.shuffle(campaign_arns)

    campaigns = []

    for campaign_arn in campaign_arns:
        campaign_region = extract_region(campaign_arn)
        personalize = get_client(service_name = 'personalize', region_name = campaign_region)
        campaign = None

        try:
            # Always try the DescribeCampaign API directly first.
            campaign = personalize.describe_campaign(campaignArn = campaign_arn)['campaign']
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug('Campaign: %s', json.dumps(campaign, indent = 2, default = str))
            _resource_cache[campaign_arn] = campaign
        except ClientError as e:
            error_code = e.response['Error']['Code']
            if error_code == 'ThrottlingException':
                logger.error('ThrottlingException trapped when calling DescribeCampaign API for %s', campaign_arn)

                # Fallback to see if we have a cached Campaign to use instead.
                campaign = _resource_cache.get(campaign_arn)
                if campaign:
                    logger.warn('Using cached campaign object for %s', campaign_arn)
                else:
                    logger.warn('Campaign %s NOT found found in cache; skipping this time', campaign_arn)
            elif error_code == 'ResourceNotFoundException':
                # Campaign has been deleted; log and skip.
                logger.error('Campaign %s no longer exists; skipping', campaign_arn)
            else:
                raise e

        if campaign:
            if campaign['status'] == 'ACTIVE':
                latest_status = None
                if campaign.get('latestCampaignUpdate'):
                    latest_status = campaign['latestCampaignUpdate']['status']

                if not latest_status or (latest_status != 'DELETE PENDING' and latest_status != 'DELETE IN_PROGRESS'):
                    campaigns.append(campaign)
                else:
                    logger.info('Campaign %s latestCampaignUpdate.status is %s and cannot be monitored in this state; skipping', campaign_arn, latest_status)
            else:
                logger.info('Campaign %s status is %s and cannot be monitored in this state; skipping', campaign_arn, campaign['status'])

    return campaigns

def get_configured_active_recommenders(event: Dict) -> List[Dict]:
    ''' Returns list of active recommenders as configured by function event and/or environment '''
    recommender_arns = determine_recommender_arns(event)

    # Shuffle the list of arns so we don't try to describe recommenders in the same order each
    # time and potentially use cached recommender details for the same recommenders further down
    # the list due to rare but possible API throttling.
    random.shuffle(recommender_arns)

    recommenders = []

    for recommender_arn in recommender_arns:
        region = extract_region(recommender_arn)
        personalize = get_client(service_name = 'personalize', region_name = region)
        recommender = None

        try:
            # Always try the DescribeRecommender API directly first.
            recommender = personalize.describe_recommender(recommenderArn = recommender_arn)['recommender']
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug('Recommender: %s', json.dumps(recommender, indent = 2, default = str))
            _resource_cache[recommender_arn] = recommender
        except ClientError as e:
            error_code = e.response['Error']['Code']
            if error_code == 'ThrottlingException':
                logger.error('ThrottlingException trapped when calling DescribeRecommender API for %s', recommender_arn)

                # Fallback to see if we have a cached Recommender to use instead.
                recommender = _resource_cache.get(recommender_arn)
                if recommender:
                    logger.warn('Using cached recommender object for %s', recommender_arn)
                else:
                    logger.warn('Recommender %s NOT found found in cache; skipping this time', recommender_arn)
            elif error_code == 'ResourceNotFoundException':
                # Recommender has been deleted; log and skip.
                logger.error('Recommender %s no longer exists; skipping', recommender_arn)
            else:
                raise e

        if recommender:
            if recommender['status'] == 'ACTIVE':
                latest_status = None
                if recommender.get('latestRecommenderUpdate'):
                    latest_status = recommender['latestRecommenderUpdate']['status']

                if not latest_status or (latest_status != 'DELETE PENDING' and latest_status != 'DELETE IN_PROGRESS'):
                    recommenders.append(recommender)
                else:
                    logger.info('Recommender %s latestRecommenderUpdate.status is %s and cannot be monitored in this state; skipping', recommender_arn, latest_status)
            else:
                logger.info('Recommender %s status is %s and cannot be monitored in this state; skipping', recommender_arn, recommender['status'])

    return recommenders