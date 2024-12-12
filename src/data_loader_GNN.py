import pandas as pd
import numpy as np
import torch
import logging
import itertools
from data_util import GraphData, HeteroData, z_norm, create_hetero_obj
from neo4j import GraphDatabase
from sklearn.preprocessing import LabelEncoder

from py2neo import Graph, Node, Relationship, NodeMatcher
def get_data(args, GBpre:True):

    '''Loads the AML transaction data from Neo4j database.
    1. The data is loaded from the dockerized database and the necessary features are chosen.
    2. The data is split into training, validation and test data.
    3. PyG Data objects are created with the respective data splits.
    '''

    # Neo4j connection details
    uri = "bolt://localhost:7687"  # Local docker container
    user = "neo4j"
    password = "password"

    # Connect to the Neo4j database
    graph = Graph(uri, auth=(user, password))

    # Fetch edges (transactions) from Neo4j

    if GBpre:
        with open('preprocessing/GBpre.cypher', 'r') as file:
            query = file.read()
    else:
        query = """
                MATCH (from)-[r:TRANSFERRED_TO]->(to)
                RETURN from.id AS from_id, 
                       to.id AS to_id, 
                       r.time_of_transaction,
                       r.amount_paid, 
                       r.currency_paid,
                       r.payment_format, 
                       r.is_laundering
                """

    result = graph.run(query)

    # Convert the result to a pandas DataFrame
    df_edges = pd.DataFrame([dict(record) for record in result])


    #TODO: This is hardcore stupid and needs to be simplified
    exploded_df = df_edges.explode('all_relationship_attributes')

    # Step 2: Convert dictionaries in the exploded column to individual columns
    attributes_df = exploded_df['all_relationship_attributes'].apply(pd.Series)

    df_edges = pd.concat([exploded_df[['from_id', 'to_id']], attributes_df], axis=1)

    # Rename all columns
    df_edges = df_edges.rename(columns={
        'amount_paid': 'Amount_Received',
        'payment_format': 'Payment_Format',
        'is_laundering': 'Is_Laundering',
        'currency_paid': 'Received_Currency',
        'time_of_transaction': 'Timestamp'
    })

    print(df_edges.head())
    logging.info(f'Available Edge Features: {df_edges.columns.tolist()}')

    df_edges['Timestamp'] = pd.to_datetime(df_edges['Timestamp'])

    df_edges['Timestamp'] = df_edges['Timestamp'] - df_edges['Timestamp'].min()
    print(f"--------{df_edges.head()}--------")

    df_edges['from_id'] = df_edges['from_id'].astype(str)
    df_edges['to_id'] = df_edges['to_id'].astype(str)

    le = LabelEncoder()

    # Applying LabelEncoder on categorical columns
    df_edges['from_id'] = le.fit_transform(df_edges['from_id'])
    df_edges['to_id'] = le.fit_transform(df_edges['to_id'])

    df_edges['from_id'] = pd.to_numeric(df_edges['from_id'], errors='coerce')
    df_edges['to_id'] = pd.to_numeric(df_edges['to_id'], errors='coerce')

    max_n_id = df_edges.loc[:, ['from_id', 'to_id']].to_numpy().max() + 1

    logging.info(f'Available Node Features / Unique Accounts: {max_n_id}')
    df_nodes = pd.DataFrame({'NodeID': np.arange(max_n_id), 'Feature': np.ones(max_n_id)})
    timestamps = torch.Tensor(df_edges['Timestamp'].dt.total_seconds().to_numpy()) #TODO: Something here is off
    logging.info(f'----- Timestamps:  {timestamps}')
    y = torch.LongTensor(df_edges['Is_Laundering'].to_numpy())

    logging.info(f"Illicit ratio = {sum(y)} / {len(y)} = {sum(y) / len(y) * 100:.2f}%")
    logging.info(f"Number of nodes (holdings doing transactions) = {df_nodes.shape[0]}")
    logging.info(f"Number of transactions = {df_edges.shape[0]}")

    edge_features = ['Timestamp', 'Amount_Received', 'Received_Currency', 'Payment_Format']
    node_features = ['Feature']

    logging.info(f'Edge features being used: {edge_features}')
    logging.info(f'Node features being used: {node_features} ("Feature" is a placeholder feature of all 1s)')
    logging.info(df_edges.dtypes)

    x = torch.tensor(df_nodes.loc[:, node_features].to_numpy()).float()
    edge_index = torch.LongTensor(df_edges.loc[:, ['from_id', 'to_id']].to_numpy().T)

    # Data Converting
    df_edges['Timestamp'] = df_edges['Timestamp'].dt.total_seconds() / ((3600 * 24)) #Really ?
    label_encoder = LabelEncoder()

    # Encode 'Payment_Format' column
    df_edges['Payment_Format'] = label_encoder.fit_transform(df_edges['Payment_Format'])

    # Encode 'Received_Currency' column
    df_edges['Received_Currency'] = label_encoder.fit_transform(df_edges['Received_Currency'])


    df_edges.to_csv("test.csv")
    edge_attr = torch.tensor(df_edges.loc[:, edge_features].to_numpy()).float()

    n_days = int(timestamps.max() / (3600 * 24) + 1)
    logging.info(f"====== {timestamps.max()} ====== {int(timestamps.max() / (3600 * 24) + 1)} ")


    n_samples = y.shape[0]
    logging.info(f'number of days and transactions in the data: {n_days} days, {n_samples} transactions')

    # data splitting
    daily_irs, weighted_daily_irs, daily_inds, daily_trans = [], [], [], []  # irs = illicit ratios, inds = indices, trans = transactions
    for day in range(n_days):
        l = day * 24 * 3600
        r = (day + 1) * 24 * 3600
        day_inds = torch.where((timestamps >= l) & (timestamps < r))[0]
        daily_irs.append(y[day_inds].float().mean())
        weighted_daily_irs.append(y[day_inds].float().mean() * day_inds.shape[0] / n_samples)
        daily_inds.append(day_inds)
        daily_trans.append(day_inds.shape[0])

    split_per = [0.6, 0.2, 0.2]
    daily_totals = np.array(daily_trans)
    d_ts = daily_totals
    I = list(range(len(d_ts)))
    split_scores = dict()

    # TODO: split_scores ist immer leer. Warum? 
    for i, j in itertools.combinations(I, 2):
        if j >= i:
            split_totals = [d_ts[:i].sum(), d_ts[i:j].sum(), d_ts[j:].sum()]
            split_totals_sum = np.sum(split_totals)
            split_props = [v / split_totals_sum for v in split_totals]
            split_error = [abs(v - t) / t for v, t in zip(split_props, split_per)]
            score = max(split_error)  # - (split_totals_sum/total) + 1
            split_scores[(i, j)] = score
        else:
            continue
    logging.info(f'[SPLIT SCORES ANALYSIS: {split_scores} ]')
    i, j = min(split_scores, key=split_scores.get)
    # split contains a list for each split (train, validation and test) and each list contains the days that are part of the respective split
    split = [list(range(i)), list(range(i, j)), list(range(j, len(daily_totals)))]
    logging.info(f'Calculate split: {split}')

    # Now, we seperate the transactions based on their indices in the timestamp array
    split_inds = {k: [] for k in range(3)}
    for i in range(3):
        for day in split[i]:
            split_inds[i].append(daily_inds[
                                     day])  # split_inds contains a list for each split (tr,val,te) which contains the indices of each day seperately

    tr_inds = torch.cat(split_inds[0])
    val_inds = torch.cat(split_inds[1])
    te_inds = torch.cat(split_inds[2])

    logging.info(f"Total train samples: {tr_inds.shape[0] / y.shape[0] * 100 :.2f}% || IR: "
                 f"{y[tr_inds].float().mean() * 100 :.2f}% || Train days: {split[0][:5]}")
    logging.info(f"Total val samples: {val_inds.shape[0] / y.shape[0] * 100 :.2f}% || IR: "
                 f"{y[val_inds].float().mean() * 100:.2f}% || Val days: {split[1][:5]}")
    logging.info(f"Total test samples: {te_inds.shape[0] / y.shape[0] * 100 :.2f}% || IR: "
                 f"{y[te_inds].float().mean() * 100:.2f}% || Test days: {split[2][:5]}")

    # Creating the final data objects
    tr_x, val_x, te_x = x, x, x
    e_tr = tr_inds.numpy()
    e_val = np.concatenate([tr_inds, val_inds])

    tr_edge_index, tr_edge_attr, tr_y, tr_edge_times = edge_index[:, e_tr], edge_attr[e_tr], y[e_tr], timestamps[e_tr]
    val_edge_index, val_edge_attr, val_y, val_edge_times = edge_index[:, e_val], edge_attr[e_val], y[e_val], timestamps[
        e_val]
    te_edge_index, te_edge_attr, te_y, te_edge_times = edge_index, edge_attr, y, timestamps

    tr_data = GraphData(x=tr_x, y=tr_y, edge_index=tr_edge_index, edge_attr=tr_edge_attr, timestamps=tr_edge_times)
    val_data = GraphData(x=val_x, y=val_y, edge_index=val_edge_index, edge_attr=val_edge_attr,
                         timestamps=val_edge_times)
    te_data = GraphData(x=te_x, y=te_y, edge_index=te_edge_index, edge_attr=te_edge_attr, timestamps=te_edge_times)

    # Adding ports and time-deltas if applicable
    if args.ports:
        logging.info(f"Start: adding ports")
        tr_data.add_ports()
        val_data.add_ports()
        te_data.add_ports()
        logging.info(f"Done: adding ports")
    if args.tds:
        logging.info(f"Start: adding time-deltas")
        tr_data.add_time_deltas()
        val_data.add_time_deltas()
        te_data.add_time_deltas()
        logging.info(f"Done: adding time-deltas")

    # Normalize data
    tr_data.x = val_data.x = te_data.x = z_norm(tr_data.x)
    if not args.model == 'rgcn':
        tr_data.edge_attr, val_data.edge_attr, te_data.edge_attr = z_norm(tr_data.edge_attr), z_norm(
            val_data.edge_attr), z_norm(te_data.edge_attr)
    else:
        tr_data.edge_attr[:, :-1], val_data.edge_attr[:, :-1], te_data.edge_attr[:, :-1] = z_norm(
            tr_data.edge_attr[:, :-1]), z_norm(val_data.edge_attr[:, :-1]), z_norm(te_data.edge_attr[:, :-1])

    # Create heterogenous if reverese MP is enabled
    if args.reverse_mp:
        tr_data = create_hetero_obj(tr_data.x, tr_data.y, tr_data.edge_index, tr_data.edge_attr, tr_data.timestamps,
                                    args)
        val_data = create_hetero_obj(val_data.x, val_data.y, val_data.edge_index, val_data.edge_attr,
                                     val_data.timestamps, args)
        te_data = create_hetero_obj(te_data.x, te_data.y, te_data.edge_index, te_data.edge_attr, te_data.timestamps,
                                    args)

    logging.info(f'train data object: {tr_data}')
    logging.info(f'validation data object: {val_data}')
    logging.info(f'test data object: {te_data}')

    return tr_data, val_data, te_data, tr_inds, val_inds, te_inds

