# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Utility Lambda function that can be used to update a Personalize campaign's minProvisionedTPS value
based on triggers such as CloudWatch event rules (i.e. cron) or application events. 
"""

import json
import boto3
import os
import json
import logging

from aws_lambda_powertools import Logger

from common import (
    extract_region,
    get_client,
    put_event
)

logger = Logger()
    
@logger.inject_lambda_context(log_event=True)
def lambda_handler(event, context):
    ''' Updates the minProvisionedTPS value for an existing Personalize campaign '''
    if event.get('detail'):
        campaign_arn = event['detail']['CampaignARN']
        min_tps = event['detail']['MinProvisionedTPS']
        reason = event['detail']['Reason']
    else:
        campaign_arn = event['CampaignARN']
        min_tps = event['MinProvisionedTPS']
        reason = event.get('Reason')

    if min_tps < 1:
        raise ValueError(f'"MinProvisionedTPS" must be >= 1')
    
    region = extract_region(campaign_arn)
    if not region:
        raise Exception('Region could not be extracted from campaign_arn')
    
    personalize = get_client(service_name = 'personalize', region_name = region)

    response = personalize.update_campaign(campaignArn = campaign_arn, minProvisionedTPS = min_tps)
    
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(json.dumps(response, indent = 2, default = str))

    if not reason:
        reason = f'Amazon Personalize campaign {campaign_arn} deletion initiated (reason unspecified)'

    put_event(
        detail_type = 'PersonalizeCampaignMinProvisionedTPSUpdated',
        detail = json.dumps({
            'CampaignARN': campaign_arn,
            'NewMinProvisionedTPS': min_tps,
            'Reason': reason
        }),
        resources = [ campaign_arn ]
    )

    logger.info({
        'campaignArn': campaign_arn,
        'minProvisionedTPS': min_tps
    })
    
    return f'Successfully initiated update of minProvisionedTPS to {min_tps} for campaign {campaign_arn}'