# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda layer functions shared across Lambda functions in this application
"""

import boto3
import os
import random

from botocore.exceptions import ClientError
from aws_lambda_powertools import Logger
from expiring_dict import ExpiringDict

logger = Logger(child=True)

_clients_by_region = {}
# Since the DescribeCampaign API easily throttles and we just need
# the minProvisionedTPS from the campaign, use a cache to help smooth
# out periods where we get throttled.
_campaign_cache = ExpiringDict(22 * 60)

PROJECT_NAME = 'PersonalizeMonitor'
ALARM_NAME_PREFIX = PROJECT_NAME + '-'

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

def extract_region(arn):
    ''' Extracts region from an AWS ARN '''
    region = None
    elements = arn.split(':')
    if len(elements) > 3:
        region = elements[3]
        
    return region

def extract_account_id(arn):
    ''' Extracts account ID from an AWS ARN '''
    account_id = None
    elements = arn.split(':')
    if len(elements) > 4:
        account_id = elements[4]
        
    return account_id

def get_client(service_name, region_name = None):
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

def determine_regions(event):
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

def determine_campaign_arns(event):
    ''' Determines Personalize campaign ARNs based on function event or environment '''

    # Check event first (list of campaign ARNs)
    arns = None
    if event:
        arns = event.get('CampaignARNs')

    if not arns:
        # Check environment variable next for list of campaign ARNs as CSV
        arns = os.environ.get('CampaignARNs')

    if not arns:
        raise Exception('"CampaignARNs" expression required in event or environment')

    if isinstance(arns, str):
        arns = [exp.strip(' ') for exp in arns.split(',')]

    logger.debug('CampaignARNs expression: %s', arns)
    
    # Look for magic value of "all" to mean all active campaigns in configured region(s)
    if len(arns) == 1 and arns[0].lower() == 'all':
        logger.debug('Retrieving ARNs for all active campaigns')
        campaign_arns = []

        # Determine regions we need to consider
        regions = determine_regions(event)
        logger.debug('Regions to scan for active campaigns: %s', regions)

        for region in regions:
            personalize = get_client(service_name = 'personalize', region_name = region)
        
            campaigns_for_region = 0

            campaigns_paginator = personalize.get_paginator('list_campaigns')
            for campaigns_page in campaigns_paginator.paginate():
                for campaign in campaigns_page['campaigns']:
                    campaign_arns.append(campaign['campaignArn'])
                    campaigns_for_region += 1

            logger.debug('Region %s has %d campaigns', region, campaigns_for_region)
    else:
        campaign_arns = arns
        
    return campaign_arns

def get_configured_active_campaigns(event):
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
            _campaign_cache[campaign_arn] = campaign
        except ClientError as e:
            error_code = e.response['Error']['Code']
            if error_code == 'ThrottlingException':
                logger.error('ThrottlingException trapped when calling DescribeCampaign API for %s', campaign_arn)

                # Fallback to see if we have a cached Campaign to use instead.
                campaign = _campaign_cache.get(campaign_arn)
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