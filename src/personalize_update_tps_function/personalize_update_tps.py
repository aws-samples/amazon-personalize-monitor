# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Utility Lambda function that can be used to update a Personalize campaign's minProvisionedTPS value
based on triggers such as CloudWatch event rules (i.e. cron) or application events.
"""

import json
import json
import logging

from aws_lambda_powertools import Logger

from common import (
    extract_region,
    extract_resource_type,
    get_client,
    put_event
)

logger = Logger()

@logger.inject_lambda_context(log_event=True)
def lambda_handler(event, _):
    ''' Updates the minProvisionedTPS value for an existing Personalize campaign '''

    if event.get('detail'):
        arn = event['detail']['ARN']
        min_tps = event['detail']['NewMinTPS']
        reason = event['detail']['Reason']
    else:
        arn = event['ARN']
        min_tps = event['NewMinTPS']
        reason = event.get('Reason')

    region = extract_region(arn)
    if not region:
        raise Exception('Region could not be extracted from ARN in event')

    resource_type = extract_resource_type(arn)
    if not resource_type:
        raise Exception('Resource type could not be extracted from ARN in event')

    if resource_type not in ['campaign', 'recommender']:
        raise Exception('Resource type represented by ARN in event is not "campaign" or "recommender"')

    if min_tps < 1:
        raise ValueError(f'"NewMinTPS" must be >= 1')

    personalize = get_client(service_name = 'personalize', region_name = region)

    if resource_type == 'campaign':
        response = personalize.update_campaign(campaignArn = arn, minProvisionedTPS = min_tps)
        notification_detail_type = 'PersonalizeCampaignMinProvisionedTPSUpdated'
    else:
        response = personalize.describe_recommender(recommenderArn = arn)

        config = response['recommender']['recommenderConfig']
        config['minRecommendationRequestsPerSecond'] = min_tps

        response = personalize.update_recommender(recommenderArn = arn, recommenderConfig = config)
        notification_detail_type = 'PersonalizeRecommenderMinRecommendationRPSUpdated'

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(json.dumps(response, indent = 2, default = str))

    if not reason:
        reason = f'Amazon Personalize {resource_type} {arn} min TPS update initiated (reason unspecified)'

    put_event(
        detail_type = notification_detail_type,
        detail = json.dumps({
            'ARN': arn,
            'NewMinTPS': min_tps,
            'Reason': reason
        }),
        resources = [ arn ]
    )

    logger.info({
        'arn': arn,
        'newMinTPS': min_tps
    })

    return f'Successfully initiated update of min TPS to {min_tps} for {resource_type} {arn}'