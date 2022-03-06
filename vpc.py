from pulumi_aws import ec2, get_availability_zones


public_subnet_cidrs = ["172.32.0.0/20", "172.32.16.0/20"]
# Use 2 AZs for our cluster
zones = ["ap-northeast-2a", "ap-northeast-2c"]

## VPC

vpc = ec2.Vpc(
    'eks-vpc',
    cidr_block='172.32.0.0/16',
    instance_tenancy='default',
    enable_dns_hostnames=True,
    enable_dns_support=True,
    tags={
        'Name': 'eks-vpc',
    },
)

igw = ec2.InternetGateway(
    'vpc-ig',
    vpc_id=vpc.id,
    tags={
        'Name': 'vpc-ig',
    },
)

eks_route_table = ec2.RouteTable(
    'vpc-route-table',
    vpc_id=vpc.id,
    routes=[ec2.RouteTableRouteArgs(
        cidr_block='0.0.0.0/0',
        gateway_id=igw.id,
    )],
    tags={
        'Name': 'vpc-rt',
    },
)

## Subnets, one for each AZ in a region

#zones = get_availability_zones()
subnet_ids = []

for zone in zones:
    vpc_subnet = ec2.Subnet(
        f'vpc-subnet-{zone}',
        assign_ipv6_address_on_creation=False,
        vpc_id=vpc.id,
        map_public_ip_on_launch=True,
        cidr_block=f'172.32.{len(subnet_ids)}.0/24',
        #cidr_block=public_subnet_cidrs,
        availability_zone=zone,
        tags={
            'Name': f'sn-{zone}',
        },
    )
    ec2.RouteTableAssociation(
        f'vpc-route-table-assoc-{zone}',
        route_table_id=eks_route_table.id,
        subnet_id=vpc_subnet.id,
    )
    subnet_ids.append(vpc_subnet.id)

## Security Group

eks_security_group = ec2.SecurityGroup(
    'eks-cluster-sg',
    vpc_id=vpc.id,
    description='Allow all HTTP(s) traffic to EKS Cluster',
    tags={
        'Name': 'pulumi-cluster-sg',
    },
    ingress=[
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=['0.0.0.0/0'],
            from_port=443,
            to_port=443,
            protocol='tcp',
            description='Allow pods to communicate with the cluster API Server.'
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=['0.0.0.0/0'],
            from_port=80,
            to_port=80,
            protocol='tcp',
            description='Allow internet access to pods'
        ),
    ],
)